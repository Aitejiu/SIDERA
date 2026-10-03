from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sidera_control_plane.application.knowledge_errors import KnowledgeAdapterError
from sidera_control_plane.application.ports import (
    ExactSectionBody,
    ExactSectionRef,
    KnowledgeCatalogPort,
    KnowledgeSourceMetadata,
    ResultInputContentPort,
)
from sidera_control_plane.application.result_input_delivery import (
    ResultInputDeliveryError,
    materialize_result_inputs,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextMaterialization,
    ContextMaterializationAudit,
    ContextMaterializationMode,
    ContextMaterializationOutcome,
    ContextMaterializationV1,
    ContextMaterializationV2,
    ContextMaterializationV3,
    ContextMetadata,
    ContextPackageManifest,
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPackageManifestV3,
    ContextPackageStatus,
    ContextPurpose,
    ContextRevocationScope,
    ContextTrustLevel,
    ControlPlaneFactSection,
    MaterializedContextSource,
    MaterializedResultInput,
    ProjectKnowledgeSection,
    SystemConstraintSection,
    TypedContextRef,
    WorkContextSection,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.work_context import WorkContextRevision

MAX_CONTEXT_PAYLOAD_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedContextFacts:
    manifest: ContextPackageManifest
    revision: WorkContextRevision
    project_knowledge: tuple[ProjectKnowledgeSection, ...]
    result_inputs: tuple[MaterializedResultInput, ...]


class ContextMaterializationError(RuntimeError):
    """A safe, classified failure while materializing a context package."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        temporary: bool = False,
        corrupt: bool = False,
    ) -> None:
        self.code = code
        self.temporary = temporary
        self.corrupt = corrupt
        super().__init__(message)


class ContextMaterializer:
    """Validate an immutable manifest and build its runtime payload in memory."""

    def __init__(
        self,
        catalog: KnowledgeCatalogPort,
        *,
        max_payload_bytes: int = MAX_CONTEXT_PAYLOAD_BYTES,
        result_input_content: ResultInputContentPort | None = None,
    ) -> None:
        if max_payload_bytes < 1:
            raise ValueError("Context materialization payload limit must be positive")
        self._catalog = catalog
        self._max_payload_bytes = max_payload_bytes
        self._result_input_content = result_input_content

    def materialize(
        self,
        manifest: ContextPackageManifest | None,
        work_context_revision: WorkContextRevision | None,
        *,
        mode: ContextMaterializationMode,
        workspace_id: UUID,
        consumer_id: UUID | None = None,
        purpose: ContextPurpose | None = None,
        context_version: int | None = None,
        expected_package_hash: str | None = None,
        owner_capability: bool = False,
        now: datetime | None = None,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ContextMaterialization:
        facts = self.validate_facts(
            manifest,
            work_context_revision,
            mode=mode,
            workspace_id=workspace_id,
            consumer_id=consumer_id,
            purpose=purpose,
            context_version=context_version,
            expected_package_hash=expected_package_hash,
            owner_capability=owner_capability,
            now=now,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        manifest = facts.manifest
        materialization = self._assemble(
            manifest,
            facts.revision,
            facts.project_knowledge,
            facts.result_inputs,
        )
        if materialization.total_byte_count > self._max_payload_bytes:
            raise ContextMaterializationError(
                "context_budget_exceeded",
                "Materialized context exceeds the hard payload limit",
            )
        if isinstance(
            materialization, (ContextMaterializationV2, ContextMaterializationV3)
        ):
            assert isinstance(
                manifest, (ContextPackageManifestV2, ContextPackageManifestV3)
            )
            if manifest.result_input_manifest is None:
                return materialization
            result_budget = manifest.result_input_manifest.budget
            if (
                materialization.total_byte_count > result_budget.max_payload_bytes
                or materialization.input_token_count > manifest.budget.max_input_tokens
            ):
                raise ContextMaterializationError(
                    "result_input_budget_exceeded",
                    "Materialized context exceeds its pinned result input budget",
                )
        return materialization

    def validate_facts(
        self,
        manifest: ContextPackageManifest | None,
        work_context_revision: WorkContextRevision | None,
        *,
        mode: ContextMaterializationMode,
        workspace_id: UUID,
        consumer_id: UUID | None = None,
        purpose: ContextPurpose | None = None,
        context_version: int | None = None,
        expected_package_hash: str | None = None,
        owner_capability: bool = False,
        now: datetime | None = None,
        correlation_id: UUID,
        caller_request_id: UUID,
        include_bodies: bool = True,
    ) -> ValidatedContextFacts:
        try:
            normalized_mode = ContextMaterializationMode(mode)
        except (TypeError, ValueError) as exc:
            raise ContextMaterializationError(
                "context_adapter_unsupported",
                "Unsupported context materialization mode",
            ) from exc
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ContextMaterializationError(
                "context_input_invalid",
                "Materialization time must be timezone aware",
            )
        if manifest is None:
            raise ContextMaterializationError(
                "context_reference_not_found",
                "Context package was not found",
            )
        if manifest.workspace_id != workspace_id:
            raise ContextMaterializationError(
                "context_workspace_mismatch",
                "Context package belongs to another workspace",
            )
        if (
            expected_package_hash is None
            or expected_package_hash != manifest.package_hash
        ):
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "Context package hash does not match the expected manifest",
            )
        if manifest.compute_package_hash() != manifest.package_hash:
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "Context package hash is internally inconsistent",
                corrupt=True,
            )
        self._validate_access(
            manifest,
            normalized_mode,
            workspace_id=workspace_id,
            consumer_id=consumer_id,
            purpose=purpose,
            context_version=context_version,
            owner_capability=owner_capability,
            now=current_time,
        )
        self._validate_revision(manifest, work_context_revision, workspace_id)
        assert work_context_revision is not None
        if not include_bodies:
            return ValidatedContextFacts(manifest, work_context_revision, (), ())
        refs = tuple(
            ExactSectionRef(
                ordinal=item.ordinal,
                page_id=item.page_id,
                revision_id=item.revision_id,
                revision_hash=item.revision_hash,
                section_key=item.section_key,
                section_hash=item.section_hash,
                source_ids=item.source_ids,
            )
            for item in manifest.knowledge_refs
        )
        bodies = self._materialize_knowledge(
            workspace_id,
            refs,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        project_knowledge = self._validate_knowledge(
            manifest,
            refs,
            bodies,
        )
        return ValidatedContextFacts(
            manifest,
            work_context_revision,
            project_knowledge,
            self.read_result_inputs(manifest),
        )

    def read_knowledge_section(
        self,
        facts: ValidatedContextFacts,
        ordinal: int,
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> ProjectKnowledgeSection:
        reference = next(
            (item for item in facts.manifest.knowledge_refs if item.ordinal == ordinal),
            None,
        )
        if reference is None:
            raise ContextMaterializationError(
                "context_reference_not_found", "Knowledge reference not found"
            )
        refs = (
            ExactSectionRef(
                ordinal=reference.ordinal,
                page_id=reference.page_id,
                revision_id=reference.revision_id,
                revision_hash=reference.revision_hash,
                section_key=reference.section_key,
                section_hash=reference.section_hash,
                source_ids=reference.source_ids,
            ),
        )
        bodies = self._materialize_knowledge(
            facts.manifest.workspace_id,
            refs,
            correlation_id=correlation_id,
            caller_request_id=caller_request_id,
        )
        return self._validate_knowledge(facts.manifest, refs, bodies, complete=False)[0]

    def read_result_inputs(
        self, manifest: ContextPackageManifest
    ) -> tuple[MaterializedResultInput, ...]:
        if not isinstance(
            manifest, (ContextPackageManifestV2, ContextPackageManifestV3)
        ):
            return ()
        if manifest.result_input_manifest is None:
            return ()
        if (
            manifest.result_input_manifest.artifacts
            and self._result_input_content is None
        ):
            raise ContextMaterializationError(
                "result_input_unavailable",
                "Result input content port is not configured",
                temporary=True,
            )
        if self._result_input_content is None:
            return ()
        try:
            return materialize_result_inputs(
                workspace_id=manifest.workspace_id,
                manifest=manifest.result_input_manifest,
                content_port=self._result_input_content,
            )
        except ResultInputDeliveryError as exc:
            raise ContextMaterializationError(
                exc.code,
                str(exc),
                temporary=exc.temporary,
                corrupt=exc.corrupt,
            ) from exc

    @staticmethod
    def _validate_access(
        manifest: ContextPackageManifest,
        mode: ContextMaterializationMode,
        *,
        workspace_id: UUID,
        consumer_id: UUID | None,
        purpose: ContextPurpose | None,
        context_version: int | None,
        owner_capability: bool,
        now: datetime,
    ) -> None:
        if mode is ContextMaterializationMode.OWNER_AUDIT and not owner_capability:
            raise ContextMaterializationError(
                "context_permission_denied",
                "Owner capability is required for context audit",
            )
        if mode is ContextMaterializationMode.CONSUMER_START:
            if consumer_id is None or purpose is None:
                raise ContextMaterializationError(
                    "context_consumer_stale",
                    "Consumer identity is required to start context",
                )
            if (
                manifest.consumer_id != consumer_id
                or manifest.purpose is not ContextPurpose(purpose)
                or manifest.consumer_type
                not in {ContextConsumerType.PLANNER, ContextConsumerType.WORKER}
            ):
                raise ContextMaterializationError(
                    "context_consumer_stale",
                    "Context package consumer identity does not match",
                )
            if context_version != manifest.context_version:
                raise ContextMaterializationError(
                    "context_consumer_stale",
                    "Context package version does not match the consumer",
                )
        elif purpose is not None and manifest.purpose is not ContextPurpose(purpose):
            raise ContextMaterializationError(
                "context_consumer_stale",
                "Context package purpose does not match the request",
            )
        if manifest.status is ContextPackageStatus.CORRUPT:
            raise ContextMaterializationError(
                "context_package_corrupt",
                "Context package is marked corrupt",
            )
        if manifest.status is ContextPackageStatus.REVOKED:
            if (
                mode is ContextMaterializationMode.OWNER_AUDIT
                and manifest.revocation_scope is ContextRevocationScope.NEW_STARTS
            ):
                pass
            else:
                raise ContextMaterializationError(
                    "context_package_revoked",
                    "Context package is revoked for this operation",
                )
        if (
            mode is ContextMaterializationMode.CONSUMER_START
            and now >= manifest.expires_at
        ):
            raise ContextMaterializationError(
                "context_package_expired",
                "Context package has expired for a new consumer start",
            )
        if manifest.workspace_id != workspace_id:
            raise ContextMaterializationError(
                "context_workspace_mismatch",
                "Context package belongs to another workspace",
            )

    @staticmethod
    def _validate_revision(
        manifest: ContextPackageManifest,
        revision: WorkContextRevision | None,
        workspace_id: UUID,
    ) -> None:
        if revision is None:
            raise ContextMaterializationError(
                "context_reference_not_found",
                "WorkContext revision was not found",
            )
        if revision.workspace_id != workspace_id or revision.workspace_id != manifest.workspace_id:
            raise ContextMaterializationError(
                "context_workspace_mismatch",
                "WorkContext revision belongs to another workspace",
            )
        if revision.revision_id != manifest.work_context_revision_id:
            raise ContextMaterializationError(
                "context_consumer_stale",
                "WorkContext revision does not match the package",
            )
        if revision.content_hash != revision.snapshot.content_hash:
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "WorkContext revision hash is internally inconsistent",
                corrupt=True,
            )

    def _materialize_knowledge(
        self,
        workspace_id: UUID,
        refs: Sequence[ExactSectionRef],
        *,
        correlation_id: UUID,
        caller_request_id: UUID,
    ) -> tuple[ExactSectionBody, ...]:
        if not refs:
            return ()
        if self._catalog is None:
            raise ContextMaterializationError(
                "context_service_unavailable",
                "Knowledge catalog is not configured",
                temporary=True,
            )
        try:
            return tuple(
                self._catalog.materialize(
                    workspace_id,
                    refs,
                    correlation_id=correlation_id,
                    caller_request_id=caller_request_id,
                )
            )
        except KnowledgeAdapterError as exc:
            code, temporary = _knowledge_error(exc)
            raise ContextMaterializationError(
                code,
                "Knowledge materialization failed",
                temporary=temporary,
                corrupt=not temporary and code in {
                    "context_hash_mismatch",
                    "context_workspace_mismatch",
                },
            ) from exc

    @staticmethod
    def _validate_knowledge(
        manifest: ContextPackageManifestV1,
        refs: Sequence[ExactSectionRef],
        bodies: Sequence[ExactSectionBody],
        *,
        complete: bool = True,
    ) -> tuple[ProjectKnowledgeSection, ...]:
        if len(bodies) != len(refs):
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "Knowledge materialization section count does not match the package",
                corrupt=True,
            )
        expected = {
            (item.page_id, item.revision_id, item.section_key): item for item in refs
        }
        if len(expected) != len(refs):
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "Context package contains duplicate knowledge references",
                corrupt=True,
            )
        actual_keys = [
            (item.page_id, item.revision_id, item.section_key) for item in bodies
        ]
        if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != set(expected):
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "Knowledge materialization references do not match the package",
                corrupt=True,
            )
        result: list[ProjectKnowledgeSection] = []
        for body in bodies:
            key = (body.page_id, body.revision_id, body.section_key)
            reference = expected[key]
            if (
                body.ordinal != reference.ordinal
                or body.revision_hash != reference.revision_hash
                or body.section_hash != reference.section_hash
                or tuple(source.source_id for source in body.sources)
                != reference.source_ids
                or body.trust != _trust_for_reference(manifest, reference)
            ):
                raise ContextMaterializationError(
                    "context_hash_mismatch",
                    "Knowledge materialization hashes or sources do not match the package",
                    corrupt=True,
                )
            body_byte_count = len(body.body_markdown.encode("utf-8"))
            if body_byte_count != body.section_byte_count:
                raise ContextMaterializationError(
                    "context_hash_mismatch",
                    "Knowledge materialization body byte count does not match",
                    corrupt=True,
                )
            if any(source.workspace_id != manifest.workspace_id for source in body.sources):
                raise ContextMaterializationError(
                    "context_workspace_mismatch",
                    "Knowledge source belongs to another workspace",
                    corrupt=True,
                )
            try:
                result.append(
                    ProjectKnowledgeSection(
                        ordinal=body.ordinal,
                        page_id=body.page_id,
                        revision_id=body.revision_id,
                        revision_hash=body.revision_hash,
                        section_key=body.section_key,
                        section_hash=body.section_hash,
                        body_markdown=body.body_markdown,
                        sources=tuple(_source(source) for source in body.sources),
                        trust_level=ContextTrustLevel(body.trust),
                        section_byte_count=body.section_byte_count,
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ContextMaterializationError(
                    "context_hash_mismatch",
                    "Knowledge materialization body is invalid",
                    corrupt=True,
                ) from exc
        expected_body_bytes = manifest.budget.actual_chars
        expected_body_tokens = manifest.budget.actual_input_tokens
        actual_body_bytes = sum(item.section_byte_count for item in result)
        actual_body_tokens = sum(_input_tokens(item.section_byte_count) for item in result)
        if (
            complete
            and (
                actual_body_bytes != expected_body_bytes
                or actual_body_tokens != expected_body_tokens
            )
            or actual_body_bytes > expected_body_bytes
            or actual_body_tokens > expected_body_tokens
        ):
            raise ContextMaterializationError(
                "context_hash_mismatch",
                "Knowledge materialization budget counts do not match the package",
                corrupt=True,
            )
        return tuple(sorted(result, key=lambda item: item.ordinal))

    def _assemble(
        self,
        manifest: ContextPackageManifest,
        revision: WorkContextRevision,
        project_knowledge: Sequence[ProjectKnowledgeSection],
        result_inputs: tuple[MaterializedResultInput, ...],
    ) -> ContextMaterialization:
        snapshot = revision.snapshot
        metadata = ContextMetadata(
            package_id=manifest.package_id,
            package_hash=manifest.package_hash,
            purpose=manifest.purpose,
            consumer_type=manifest.consumer_type,
            consumer_id=manifest.consumer_id,
            context_version=manifest.context_version,
            expires_at=manifest.expires_at,
        )
        system_constraints = SystemConstraintSection(
            objective=snapshot.objective,
            constraints=snapshot.constraints,
            acceptance_criteria=snapshot.acceptance_criteria,
        )
        work_context = WorkContextSection(
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            content_hash=revision.content_hash,
            snapshot_schema_version=snapshot.schema_version,
            requested_outputs=snapshot.requested_outputs,
            clarification_decisions=snapshot.clarification_decisions,
            accepted_decisions=snapshot.accepted_decisions,
            unresolved_questions=snapshot.unresolved_questions,
            checkpoint_refs=snapshot.checkpoint_refs,
            missing_fact_codes=snapshot.missing_fact_codes,
        )
        control_plane_facts = ControlPlaneFactSection(
            facts=manifest.control_plane_fact_snapshot,
        )
        artifact_and_evidence_refs = tuple(
            [
                TypedContextRef(
                    category="artifact",
                    ref_id=item.ref_id,
                    ref_type=item.ref_type,
                    ordinal=item.ordinal,
                )
                for item in manifest.artifact_refs
            ]
            + [
                TypedContextRef(
                    category="evidence",
                    ref_id=item.ref_id,
                    ref_type=item.ref_type,
                    ordinal=item.ordinal,
                )
                for item in manifest.evidence_refs
            ]
        )
        if not isinstance(manifest, (ContextPackageManifestV2, ContextPackageManifestV3)):
            return ContextMaterializationV1(
                metadata=metadata,
                system_constraints=system_constraints,
                work_context=work_context,
                control_plane_facts=control_plane_facts,
                project_knowledge=tuple(project_knowledge),
                artifact_and_evidence_refs=artifact_and_evidence_refs,
            )
        if isinstance(manifest, ContextPackageManifestV3):
            return ContextMaterializationV3(
                metadata=metadata,
                system_constraints=system_constraints,
                work_context=work_context,
                control_plane_facts=control_plane_facts,
                project_knowledge=tuple(project_knowledge),
                artifact_and_evidence_refs=artifact_and_evidence_refs,
                result_inputs=result_inputs,
                code_input_manifest=manifest.code_input_manifest,
                verification_manifest=manifest.verification_manifest,
            )
        return ContextMaterializationV2(
            metadata=metadata,
            system_constraints=system_constraints,
            work_context=work_context,
            control_plane_facts=control_plane_facts,
            project_knowledge=tuple(project_knowledge),
            artifact_and_evidence_refs=artifact_and_evidence_refs,
            result_inputs=result_inputs,
        )


def build_materialization_audit(
    manifest: ContextPackageManifest,
    *,
    attempt: int,
    outcome: ContextMaterializationOutcome,
    occurred_at: datetime,
    materialization: ContextMaterialization | None = None,
    failure_code: str | None = None,
    audit_id: UUID | None = None,
) -> ContextMaterializationAudit:
    normalized_outcome = ContextMaterializationOutcome(outcome)
    if normalized_outcome is ContextMaterializationOutcome.SUCCEEDED:
        if materialization is None:
            raise ValueError("successful materialization audit requires a payload")
        section_count = len(materialization.project_knowledge)
        if isinstance(materialization, (ContextMaterializationV2, ContextMaterializationV3)):
            section_count += len(materialization.result_inputs)
        byte_count = materialization.total_byte_count
        input_token_count = materialization.input_token_count
        normalized_failure_code = None
    else:
        section_count = 0
        byte_count = 0
        input_token_count = 0
        normalized_failure_code = failure_code or "context_materialization_failed"
    return ContextMaterializationAudit(
        audit_id=audit_id or uuid4(),
        package_id=manifest.package_id,
        workspace_id=manifest.workspace_id,
        consumer_type=manifest.consumer_type,
        consumer_id=manifest.consumer_id,
        context_version=manifest.context_version,
        attempt=attempt,
        outcome=normalized_outcome,
        section_count=section_count,
        byte_count=byte_count,
        input_token_count=input_token_count,
        failure_code=normalized_failure_code,
        occurred_at=occurred_at,
    )


def _trust_for_reference(
    manifest: ContextPackageManifestV1,
    reference: ExactSectionRef,
) -> str:
    for item in manifest.knowledge_refs:
        if (
            item.page_id == reference.page_id
            and item.revision_id == reference.revision_id
            and item.section_key == reference.section_key
        ):
            return item.trust_level.value
    raise ContextMaterializationError(
        "context_reference_not_found",
        "Knowledge reference was not found in the package",
        corrupt=True,
    )


def _source(source: KnowledgeSourceMetadata) -> MaterializedContextSource:
    return MaterializedContextSource(
        source_id=source.source_id,
        workspace_id=source.workspace_id,
        source_type=source.source_type,
        label=source.label,
        uri=source.uri,
        workspace_relative_path=source.workspace_relative_path,
        git_revision=source.git_revision,
        digest=source.digest,
        metadata=source.metadata,
        availability=source.availability,
        safe_reason_code=source.safe_reason_code,
        last_checked_at=source.last_checked_at,
        version=source.version,
    )


def _input_tokens(byte_count: int) -> int:
    return (byte_count + 3) // 4


def _knowledge_error(error: KnowledgeAdapterError) -> tuple[str, bool]:
    if error.temporary or error.code in {"temporary_unavailable", "service_unavailable"}:
        return "context_service_unavailable", True
    return {
        "reference_not_found": "context_reference_not_found",
        "workspace_mismatch": "context_workspace_mismatch",
        "permanent_mismatch": "context_hash_mismatch",
        "permission_denied": "context_permission_denied",
    }.get(error.code, "context_service_unavailable"), False
