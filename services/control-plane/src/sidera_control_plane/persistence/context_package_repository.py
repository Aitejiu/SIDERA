from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import EventStorePort
from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextBudgetV1,
    ContextConsumerType,
    ContextMaterializationAudit,
    ContextMaterializationOutcome,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPackageStatus,
    ContextPurpose,
    ContextRevocationScope,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.work_context import WorkReference
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ContextPackageAcknowledgementModel,
    ContextPackageKnowledgeRefModel,
    ContextPackageMaterializationAuditModel,
    ContextPackageModel,
    EventModel,
)


class ContextAssemblyRepository:
    def __init__(self, session: Session, events: EventStorePort | None = None) -> None:
        self._session = session
        self._events = events

    def add(self, request: ContextAssemblyRequest) -> ContextAssemblyRequest:
        existing = self._session.scalar(
            select(ContextAssemblyRequestModel)
            .where(
                or_(
                    and_(
                        ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                        ContextAssemblyRequestModel.purpose == request.purpose.value,
                        ContextAssemblyRequestModel.consumer_id == request.consumer_id,
                    ),
                    and_(
                        ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                        ContextAssemblyRequestModel.idempotency_key == request.idempotency_key,
                    ),
                )
            )
            .limit(1)
        )
        if existing is not None:
            stored = _to_assembly_request(existing)
            if (
                stored.request_fingerprint != request.request_fingerprint
                or stored.idempotency_key != request.idempotency_key
            ):
                raise ValidationError("context assembly fingerprint or idempotency key conflicts")
            return stored
        self._session.add(_assembly_request_model(request))
        self._session.flush()
        return request

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextAssemblyRequest | None:
        statement = select(ContextAssemblyRequestModel).where(
            ContextAssemblyRequestModel.workspace_id == workspace_id,
            ContextAssemblyRequestModel.purpose == ContextPurpose(purpose).value,
            ContextAssemblyRequestModel.consumer_id == consumer_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_assembly_request(row) if row is not None else None

    def get_for_update(
        self, request_id: UUID, workspace_id: UUID
    ) -> ContextAssemblyRequest | None:
        row = self._session.scalar(
            select(ContextAssemblyRequestModel)
            .where(
                ContextAssemblyRequestModel.id == request_id,
                ContextAssemblyRequestModel.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        return _to_assembly_request(row) if row is not None else None

    def rebind_pending(
        self,
        request: ContextAssemblyRequest,
        *,
        expected_version: int,
        now: datetime,
    ) -> ContextAssemblyRequest:
        if request.status not in {
            ContextAssemblyStatus.QUEUED,
            ContextAssemblyStatus.ASSEMBLING,
        }:
            raise ValidationError(
                "only queued or assembling context assemblies can be rebound"
            )
        result = self._session.execute(
            update(ContextAssemblyRequestModel)
            .where(
                ContextAssemblyRequestModel.id == request.request_id,
                ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                ContextAssemblyRequestModel.version == expected_version,
                ContextAssemblyRequestModel.status.in_(
                    (
                        ContextAssemblyStatus.QUEUED.value,
                        ContextAssemblyStatus.ASSEMBLING.value,
                    )
                ),
            )
            .values(
                status=ContextAssemblyStatus.QUEUED.value,
                work_context_revision_id=request.work_context_revision_id,
                knowledge_search_scope=_plain_json(request.knowledge_search_scope),
                knowledge_filter_refs=_plain_json(request.knowledge_filter_refs),
                request_fingerprint=request.request_fingerprint,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                error_code=None,
                version=expected_version + 1,
                updated_at=now,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(expected_version, self._current_version(request))
        self._session.flush()
        updated = self.get_for_update(request.request_id, request.workspace_id)
        if updated is None:
            raise ValidationError("context assembly disappeared after rebind")
        return updated

    def reset_for_retry(
        self,
        request: ContextAssemblyRequest,
        *,
        expected_version: int,
        now: datetime,
    ) -> ContextAssemblyRequest:
        if request.status not in {
            ContextAssemblyStatus.BLOCKED,
            ContextAssemblyStatus.RETRY_WAIT,
        }:
            raise ValidationError("only blocked or retry_wait context assemblies can be retried")
        result = self._session.execute(
            update(ContextAssemblyRequestModel)
            .where(
                ContextAssemblyRequestModel.id == request.request_id,
                ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                ContextAssemblyRequestModel.version == expected_version,
                ContextAssemblyRequestModel.status.in_(
                    (
                        ContextAssemblyStatus.BLOCKED.value,
                        ContextAssemblyStatus.RETRY_WAIT.value,
                    )
                ),
            )
            .values(
                status=ContextAssemblyStatus.QUEUED.value,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=now,
                error_code=None,
                version=expected_version + 1,
                updated_at=now,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(expected_version, self._current_version(request))
        self._session.flush()
        updated = self.get_for_update(request.request_id, request.workspace_id)
        if updated is None:
            raise ValidationError("context assembly disappeared after retry")
        return updated

    def claim_next(
        self,
        *,
        workspace_id: UUID | None = None,
        owner: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[ContextAssemblyRequest, ...]:
        if not owner.strip():
            raise ValidationError("context assembly lease owner is required")
        if lease_ttl <= timedelta(0):
            raise ValidationError("context assembly lease ttl must be positive")
        if limit < 1:
            raise ValidationError("context assembly claim limit must be positive")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("context assembly claim time must be timezone aware")
        claimable = or_(
            and_(
                ContextAssemblyRequestModel.status.in_(
                    (
                        ContextAssemblyStatus.QUEUED.value,
                        ContextAssemblyStatus.RETRY_WAIT.value,
                    )
                ),
                or_(
                    ContextAssemblyRequestModel.next_attempt_at.is_(None),
                    ContextAssemblyRequestModel.next_attempt_at <= now,
                ),
            ),
            and_(
                ContextAssemblyRequestModel.status
                == ContextAssemblyStatus.ASSEMBLING.value,
                ContextAssemblyRequestModel.lease_expires_at <= now,
            ),
        )
        filters = [claimable]
        if workspace_id is not None:
            filters.insert(0, ContextAssemblyRequestModel.workspace_id == workspace_id)
        rows = self._session.scalars(
            select(ContextAssemblyRequestModel)
            .where(*filters)
            .order_by(
                ContextAssemblyRequestModel.created_at.asc(),
                ContextAssemblyRequestModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        claimed: list[ContextAssemblyRequest] = []
        for row in rows:
            previous_status = ContextAssemblyStatus(row.status)
            previous_version = row.version
            if previous_status is not ContextAssemblyStatus.QUEUED:
                row.attempt += 1
            row.status = ContextAssemblyStatus.ASSEMBLING.value
            row.lease_owner = owner.strip()
            row.lease_expires_at = now + lease_ttl
            row.next_attempt_at = None
            row.error_code = None
            row.version += 1
            row.updated_at = now
            del previous_version
            claimed.append(_to_assembly_request(row))
        self._session.flush()
        return tuple(claimed)

    def save_retry(
        self,
        request: ContextAssemblyRequest,
        *,
        owner: str,
        expected_version: int,
        next_attempt_at: datetime,
        error_code: str,
        now: datetime | None = None,
    ) -> ContextAssemblyRequest:
        timestamp = now or datetime.now(UTC)
        updated = self._save_claimed(
            request,
            owner=owner,
            expected_version=expected_version,
            status=ContextAssemblyStatus.RETRY_WAIT,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
            now=timestamp,
        )
        return updated

    def save_blocked(
        self,
        request: ContextAssemblyRequest,
        *,
        owner: str,
        expected_version: int,
        error_code: str,
        now: datetime | None = None,
    ) -> ContextAssemblyRequest:
        timestamp = now or datetime.now(UTC)
        return self._save_claimed(
            request,
            owner=owner,
            expected_version=expected_version,
            status=ContextAssemblyStatus.BLOCKED,
            error_code=error_code,
            next_attempt_at=None,
            now=timestamp,
        )

    def complete_with_package(
        self,
        request: ContextAssemblyRequest,
        manifest: ContextPackageManifestV1,
        *,
        owner: str,
        expected_version: int,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifestV1:
        if manifest.workspace_id != request.workspace_id:
            raise ValidationError("context package workspace does not match assembly")
        if manifest.consumer_id != request.consumer_id or manifest.purpose is not request.purpose:
            raise ValidationError("context package consumer does not match assembly")
        if manifest.work_context_revision_id != request.work_context_revision_id:
            raise ValidationError("context package revision does not match assembly")
        if manifest.idempotency_key != request.idempotency_key:
            raise ValidationError("context package idempotency key does not match assembly")
        self._session.add(_package_model(request, manifest))
        self._session.flush()
        for knowledge_ref in manifest.knowledge_refs:
            self._session.add(
                ContextPackageKnowledgeRefModel(
                    package_id=manifest.package_id,
                    ordinal=knowledge_ref.ordinal,
                    page_id=knowledge_ref.page_id,
                    revision_id=knowledge_ref.revision_id,
                    revision_hash=knowledge_ref.revision_hash,
                    section_key=knowledge_ref.section_key,
                    section_hash=knowledge_ref.section_hash,
                    source_ids=[str(item) for item in knowledge_ref.source_ids],
                    selection_reason=knowledge_ref.selection_reason.value,
                    trust_level=knowledge_ref.trust_level.value,
                )
            )
        result = self._session.execute(
            update(ContextAssemblyRequestModel)
            .where(
                ContextAssemblyRequestModel.id == request.request_id,
                ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                ContextAssemblyRequestModel.version == expected_version,
                ContextAssemblyRequestModel.status == ContextAssemblyStatus.ASSEMBLING.value,
                ContextAssemblyRequestModel.lease_owner == owner,
            )
            .values(
                status=ContextAssemblyStatus.READY.value,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                error_code=None,
                version=expected_version + 1,
                updated_at=manifest.created_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(expected_version, self._current_version(request))
        self._append_event(
            event_type="ContextPackageCreated",
            manifest=manifest,
            payload={
                "package_id": str(manifest.package_id),
                "assembly_request_id": str(request.request_id),
                "package_hash": manifest.package_hash,
                "work_context_revision_id": str(manifest.work_context_revision_id),
                "knowledge_ref_count": len(manifest.knowledge_refs),
                "budget_actual": {
                    "sections": manifest.budget.actual_sections,
                    "chars": manifest.budget.actual_chars,
                    "input_tokens": manifest.budget.actual_input_tokens,
                },
            },
            actor_type=actor_type,
            actor_id=actor_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self._session.flush()
        return manifest

    def _save_claimed(
        self,
        request: ContextAssemblyRequest,
        *,
        owner: str,
        expected_version: int,
        status: ContextAssemblyStatus,
        error_code: str,
        next_attempt_at: datetime | None,
        now: datetime,
    ) -> ContextAssemblyRequest:
        result = self._session.execute(
            update(ContextAssemblyRequestModel)
            .where(
                ContextAssemblyRequestModel.id == request.request_id,
                ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                ContextAssemblyRequestModel.version == expected_version,
                ContextAssemblyRequestModel.status == ContextAssemblyStatus.ASSEMBLING.value,
                ContextAssemblyRequestModel.lease_owner == owner,
                or_(
                    ContextAssemblyRequestModel.lease_expires_at.is_(None),
                    ContextAssemblyRequestModel.lease_expires_at > now,
                ),
            )
            .values(
                status=status.value,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=next_attempt_at,
                error_code=error_code,
                version=expected_version + 1,
                updated_at=now,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(expected_version, self._current_version(request))
        self._session.flush()
        row = self._session.scalar(
            select(ContextAssemblyRequestModel).where(
                ContextAssemblyRequestModel.id == request.request_id,
                ContextAssemblyRequestModel.workspace_id == request.workspace_id,
            )
        )
        if row is None:
            raise ValidationError("context assembly disappeared after update")
        return _to_assembly_request(row)

    def _current_version(self, request: ContextAssemblyRequest) -> int:
        return int(
            self._session.scalar(
                select(ContextAssemblyRequestModel.version).where(
                    ContextAssemblyRequestModel.id == request.request_id,
                    ContextAssemblyRequestModel.workspace_id == request.workspace_id,
                )
            )
            or 0
        )

    def _append_event(
        self,
        *,
        event_type: str,
        manifest: ContextPackageManifestV1,
        payload: dict[str, object],
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        if self._events is None:
            return
        self._events.append_with_outbox(
            EventData(
                id=uuid4(),
                workspace_id=manifest.workspace_id,
                aggregate_type="context_package",
                aggregate_id=manifest.package_id,
                aggregate_version=1,
                event_type=event_type,
                payload=payload,
                actor_type=actor_type,
                actor_id=actor_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )
        )


class ContextPackageRepository:
    def __init__(self, session: Session, events: EventStorePort | None = None) -> None:
        self._session = session
        self._events = events

    def get(
        self, package_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> ContextPackageManifestV1 | None:
        statement = select(ContextPackageModel).where(
            ContextPackageModel.id == package_id,
            ContextPackageModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return self._to_manifest(row) if row is not None else None

    def get_for_consumer(
        self,
        purpose: ContextPurpose,
        consumer_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> ContextPackageManifestV1 | None:
        statement = select(ContextPackageModel).where(
            ContextPackageModel.purpose == ContextPurpose(purpose).value,
            ContextPackageModel.consumer_id == consumer_id,
            ContextPackageModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return self._to_manifest(row) if row is not None else None

    def add_ack(
        self,
        acknowledgement: ContextAcknowledgement,
        *,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextAcknowledgement:
        package_workspace = self._package_workspace(acknowledgement.package_id)
        package = self.get(
            acknowledgement.package_id,
            package_workspace,
            for_update=True,
        )
        if package is None:
            raise ValidationError("context package not found")
        if package.package_hash != acknowledgement.package_hash:
            raise ValidationError("context acknowledgement package hash conflicts")
        existing = self._session.get(
            ContextPackageAcknowledgementModel,
            (
                acknowledgement.package_id,
                acknowledgement.consumer_type.value,
                acknowledgement.consumer_id,
            ),
        )
        if existing is not None:
            if existing.acknowledged_package_hash != acknowledgement.package_hash:
                raise ValidationError("context acknowledgement package hash conflicts")
            return _to_acknowledgement(existing)
        self._session.add(
            ContextPackageAcknowledgementModel(
                package_id=acknowledgement.package_id,
                workspace_id=package_workspace,
                consumer_type=acknowledgement.consumer_type.value,
                consumer_id=acknowledgement.consumer_id,
                context_version=acknowledgement.context_version,
                acknowledged_package_hash=acknowledgement.package_hash,
                adapter_module_instance_id=acknowledgement.adapter_module_id,
                adapter_config_version=acknowledgement.adapter_config_version,
                acknowledged_at=acknowledgement.acknowledged_at,
            )
        )
        self._append_package_event(
            event_type="ContextPackageAcknowledged",
            package_id=acknowledgement.package_id,
            workspace_id=package_workspace,
            payload={
                "package_id": str(acknowledgement.package_id),
                "package_hash": acknowledgement.package_hash,
                "consumer_type": acknowledgement.consumer_type.value,
                "consumer_id": str(acknowledgement.consumer_id),
                "context_version": acknowledgement.context_version,
                "schema_version": acknowledgement.schema_version,
                "operation_id": (
                    str(acknowledgement.operation_id)
                    if acknowledgement.operation_id is not None
                    else None
                ),
                "adapter_module_instance_id": str(acknowledgement.adapter_module_id),
                "adapter_config_version": acknowledgement.adapter_config_version,
            },
            actor_type=actor_type,
            actor_id=actor_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self._session.flush()
        return acknowledgement

    def append_materialization_audit(
        self,
        audit: ContextMaterializationAudit,
        *,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        package = self.get(audit.package_id, audit.workspace_id, for_update=True)
        if package is None:
            raise ValidationError("context package not found for materialization audit")
        existing = self._session.scalar(
            select(ContextPackageMaterializationAuditModel).where(
                ContextPackageMaterializationAuditModel.package_id == audit.package_id,
                ContextPackageMaterializationAuditModel.consumer_type
                == audit.consumer_type.value,
                ContextPackageMaterializationAuditModel.consumer_id == audit.consumer_id,
                ContextPackageMaterializationAuditModel.attempt == audit.attempt,
            )
        )
        if existing is not None:
            if (
                existing.outcome != audit.outcome.value
                or existing.failure_code != audit.failure_code
            ):
                raise ValidationError("context materialization audit conflicts")
            return
        self._session.add(
            ContextPackageMaterializationAuditModel(
                id=audit.audit_id,
                package_id=audit.package_id,
                workspace_id=audit.workspace_id,
                consumer_type=audit.consumer_type.value,
                consumer_id=audit.consumer_id,
                context_version=audit.context_version,
                attempt=audit.attempt,
                outcome=audit.outcome.value,
                section_count=audit.section_count,
                byte_count=audit.byte_count,
                input_token_count=audit.input_token_count,
                failure_code=audit.failure_code,
                occurred_at=audit.occurred_at,
            )
        )
        if audit.outcome is ContextMaterializationOutcome.SUCCEEDED and package.materialized_at is None:
            self._session.execute(
                update(ContextPackageModel)
                .where(
                    ContextPackageModel.id == audit.package_id,
                    ContextPackageModel.workspace_id == audit.workspace_id,
                )
                .values(materialized_at=audit.occurred_at, updated_at=audit.occurred_at)
            )
        self._append_package_event(
            event_type="ContextPackageMaterialized",
            package_id=audit.package_id,
            workspace_id=audit.workspace_id,
            payload={
                "package_id": str(audit.package_id),
                "consumer_type": audit.consumer_type.value,
                "consumer_id": str(audit.consumer_id),
                "context_version": audit.context_version,
                "attempt": audit.attempt,
                "outcome": audit.outcome.value,
                "section_count": audit.section_count,
                "byte_count": audit.byte_count,
                "input_token_count": audit.input_token_count,
                "failure_code": audit.failure_code,
            },
            actor_type=actor_type,
            actor_id=actor_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self._session.flush()

    def revoke(
        self,
        package_id: UUID,
        workspace_id: UUID,
        *,
        expected_package_hash: str,
        reason: str,
        scope: str,
        now: datetime,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifestV1:
        package = self.get(package_id, workspace_id, for_update=True)
        if package is None:
            raise ValidationError("context package not found")
        if package.package_hash != expected_package_hash:
            raise ValidationError("context package hash conflicts")
        normalized_scope = ContextRevocationScope(scope)
        if package.status is ContextPackageStatus.REVOKED:
            return package
        if package.status is ContextPackageStatus.CORRUPT:
            raise ValidationError("corrupt context package cannot be revoked")
        self._session.execute(
            update(ContextPackageModel)
            .where(
                ContextPackageModel.id == package_id,
                ContextPackageModel.workspace_id == workspace_id,
                ContextPackageModel.status == ContextPackageStatus.ACTIVE.value,
                ContextPackageModel.package_hash == expected_package_hash,
            )
            .values(
                status=ContextPackageStatus.REVOKED.value,
                revoked_at=now,
                revocation_reason=reason,
                revocation_scope=normalized_scope.value,
                updated_at=now,
            )
        )
        self._append_package_event(
            event_type="ContextPackageRevoked",
            package_id=package_id,
            workspace_id=workspace_id,
            payload={
                "package_id": str(package_id),
                "package_hash": expected_package_hash,
                "reason": reason,
                "scope": normalized_scope.value,
            },
            actor_type=actor_type,
            actor_id=actor_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        return replace(
            package,
            status=ContextPackageStatus.REVOKED,
            revoked_at=now,
            revocation_reason=reason,
            revocation_scope=normalized_scope,
        )

    def mark_corrupt(
        self,
        package_id: UUID,
        workspace_id: UUID,
        *,
        reason: str,
        now: datetime,
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> ContextPackageManifestV1:
        package = self.get(package_id, workspace_id, for_update=True)
        if package is None:
            raise ValidationError("context package not found")
        if package.status is ContextPackageStatus.CORRUPT:
            return package
        if package.status is ContextPackageStatus.REVOKED:
            raise ValidationError("revoked context package cannot be marked corrupt")
        self._session.execute(
            update(ContextPackageModel)
            .where(
                ContextPackageModel.id == package_id,
                ContextPackageModel.workspace_id == workspace_id,
                ContextPackageModel.status == ContextPackageStatus.ACTIVE.value,
            )
            .values(
                status=ContextPackageStatus.CORRUPT.value,
                revocation_reason=reason,
                updated_at=now,
            )
        )
        self._append_package_event(
            event_type="ContextPackageCorrupt",
            package_id=package_id,
            workspace_id=workspace_id,
            payload={
                "package_id": str(package_id),
                "package_hash": package.package_hash,
                "reason": reason,
            },
            actor_type=actor_type,
            actor_id=actor_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self._session.flush()
        stored = self.get(package_id, workspace_id)
        if stored is None:
            raise ValidationError("context package disappeared after corrupt mark")
        return stored

    def _package_workspace(self, package_id: UUID) -> UUID:
        workspace_id = self._session.scalar(
            select(ContextPackageModel.workspace_id).where(ContextPackageModel.id == package_id)
        )
        if workspace_id is None:
            raise ValidationError("context package not found")
        return workspace_id

    def _append_package_event(
        self,
        *,
        event_type: str,
        package_id: UUID,
        workspace_id: UUID,
        payload: dict[str, object],
        actor_type: str,
        actor_id: str,
        causation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        if self._events is None:
            return
        self._events.append_with_outbox(
            EventData(
                id=uuid4(),
                workspace_id=workspace_id,
                aggregate_type="context_package",
                aggregate_id=package_id,
                aggregate_version=self._next_package_event_version(
                    package_id, workspace_id
                ),
                event_type=event_type,
                payload=payload,
                actor_type=actor_type,
                actor_id=actor_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )
        )

    def _next_package_event_version(self, package_id: UUID, workspace_id: UUID) -> int:
        latest = self._session.scalar(
            select(func.max(EventModel.aggregate_version)).where(
                EventModel.aggregate_type == "context_package",
                EventModel.aggregate_id == package_id,
                EventModel.workspace_id == workspace_id,
            )
        )
        return int(latest or 0) + 1

    def _to_manifest(self, row: ContextPackageModel) -> ContextPackageManifestV1:
        refs = self._session.scalars(
            select(ContextPackageKnowledgeRefModel)
            .where(ContextPackageKnowledgeRefModel.package_id == row.id)
            .order_by(ContextPackageKnowledgeRefModel.ordinal.asc())
        ).all()
        manifest = ContextPackageManifestV1(
            package_id=row.id,
            workspace_id=row.workspace_id,
            purpose=ContextPurpose(row.purpose),
            consumer_type=ContextConsumerType(row.consumer_type),
            consumer_id=row.consumer_id,
            worker_item_id=row.worker_item_id,
            work_context_revision_id=row.work_context_revision_id,
            context_version=row.context_version,
            control_plane_fact_snapshot=_mapping(row.control_plane_fact_snapshot, "control_plane_fact_snapshot"),
            knowledge_refs=tuple(_to_knowledge_ref(item) for item in refs),
            artifact_refs=_to_work_refs(row.artifact_refs, "artifact_refs"),
            evidence_refs=_to_work_refs(row.evidence_refs, "evidence_refs"),
            budget=_to_budget(row.budget),
            expires_at=row.expires_at,
            idempotency_key=row.idempotency_key,
            status=ContextPackageStatus(row.status),
            created_at=row.created_at,
            materialized_at=row.materialized_at,
            revoked_at=row.revoked_at,
            revocation_reason=row.revocation_reason,
            revocation_scope=(
                ContextRevocationScope(row.revocation_scope)
                if row.revocation_scope is not None
                else None
            ),
        )
        if manifest.package_hash != row.package_hash:
            raise ValidationError("stored context package hash does not match manifest")
        return manifest


def _assembly_request_model(request: ContextAssemblyRequest) -> ContextAssemblyRequestModel:
    return ContextAssemblyRequestModel(
        id=request.request_id,
        workspace_id=request.workspace_id,
        purpose=request.purpose.value,
        consumer_type=request.consumer_type.value,
        consumer_id=request.consumer_id,
        worker_item_id=request.worker_item_id,
        work_context_revision_id=request.work_context_revision_id,
        context_version=request.context_version,
        knowledge_search_scope=_plain_json(request.knowledge_search_scope),
        knowledge_filter_refs=_plain_json(request.knowledge_filter_refs),
        fact_scope_refs=_plain_json(request.fact_scope_refs),
        budget=_plain_json(request.budget),
        expires_at=request.expires_at,
        status=request.status.value,
        lease_owner=request.lease_owner,
        lease_expires_at=request.lease_expires_at,
        attempt=request.attempt,
        next_attempt_at=request.next_attempt_at,
        error_code=request.error_code,
        request_fingerprint=request.request_fingerprint,
        idempotency_key=request.idempotency_key,
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def _to_assembly_request(row: ContextAssemblyRequestModel) -> ContextAssemblyRequest:
    return ContextAssemblyRequest(
        request_id=row.id,
        workspace_id=row.workspace_id,
        purpose=ContextPurpose(row.purpose),
        consumer_type=ContextConsumerType(row.consumer_type),
        consumer_id=row.consumer_id,
        worker_item_id=row.worker_item_id,
        work_context_revision_id=row.work_context_revision_id,
        context_version=row.context_version,
        knowledge_search_scope=_mapping(row.knowledge_search_scope, "knowledge_search_scope"),
        knowledge_filter_refs=_sequence(row.knowledge_filter_refs, "knowledge_filter_refs"),
        fact_scope_refs=_sequence(row.fact_scope_refs, "fact_scope_refs"),
        budget=_mapping(row.budget, "budget"),
        expires_at=row.expires_at,
        status=ContextAssemblyStatus(row.status),
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        attempt=row.attempt,
        next_attempt_at=row.next_attempt_at,
        error_code=row.error_code,
        request_fingerprint=row.request_fingerprint,
        idempotency_key=row.idempotency_key,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _package_model(
    request: ContextAssemblyRequest, manifest: ContextPackageManifestV1
) -> ContextPackageModel:
    return ContextPackageModel(
        id=manifest.package_id,
        workspace_id=manifest.workspace_id,
        assembly_request_id=request.request_id,
        purpose=manifest.purpose.value,
        consumer_type=manifest.consumer_type.value,
        consumer_id=manifest.consumer_id,
        worker_item_id=manifest.worker_item_id,
        work_context_revision_id=manifest.work_context_revision_id,
        context_version=manifest.context_version,
        control_plane_fact_snapshot=_plain_json(manifest.control_plane_fact_snapshot),
        artifact_refs=_plain_json([item.to_mapping() for item in manifest.artifact_refs]),
        evidence_refs=_plain_json([item.to_mapping() for item in manifest.evidence_refs]),
        budget=_plain_json(manifest.budget.to_mapping()),
        expires_at=manifest.expires_at,
        package_hash=manifest.package_hash,
        status=manifest.status.value,
        revoked_at=manifest.revoked_at,
        revocation_reason=manifest.revocation_reason,
        revocation_scope=(manifest.revocation_scope.value if manifest.revocation_scope else None),
        idempotency_key=manifest.idempotency_key,
        materialized_at=manifest.materialized_at,
        created_at=manifest.created_at,
        updated_at=manifest.created_at,
    )


def _to_acknowledgement(
    row: ContextPackageAcknowledgementModel,
) -> ContextAcknowledgement:
    return ContextAcknowledgement(
        package_id=row.package_id,
        package_hash=row.acknowledged_package_hash,
        consumer_type=ContextConsumerType(row.consumer_type),
        consumer_id=row.consumer_id,
        context_version=row.context_version,
        adapter_module_id=row.adapter_module_instance_id,
        adapter_config_version=row.adapter_config_version,
        acknowledged_at=row.acknowledged_at,
    )


def _to_knowledge_ref(row: ContextPackageKnowledgeRefModel) -> ContextPackageKnowledgeRef:
    return ContextPackageKnowledgeRef(
        page_id=row.page_id,
        revision_id=row.revision_id,
        revision_hash=row.revision_hash,
        section_key=row.section_key,
        section_hash=row.section_hash,
        source_ids=tuple(UUID(str(item)) for item in _sequence(row.source_ids, "source_ids")),
        selection_reason=ContextSelectionReason(row.selection_reason),
        trust_level=ContextTrustLevel(row.trust_level),
        ordinal=row.ordinal,
    )


def _to_budget(value: object) -> ContextBudgetV1:
    mapping = _mapping(value, "budget")
    return ContextBudgetV1(
        profile_id=UUID(str(mapping["profile_id"])),
        profile_version=_integer(mapping["profile_version"], "profile_version"),
        max_sections=_integer(mapping["max_sections"], "max_sections"),
        max_chars=_integer(mapping["max_chars"], "max_chars"),
        max_input_tokens=_integer(mapping["max_input_tokens"], "max_input_tokens"),
        actual_sections=_integer(mapping.get("actual_sections", 0), "actual_sections"),
        actual_chars=_integer(mapping.get("actual_chars", 0), "actual_chars"),
        actual_input_tokens=_integer(
            mapping.get("actual_input_tokens", 0), "actual_input_tokens"
        ),
    )


def _to_work_refs(value: object, field_name: str) -> tuple[WorkReference, ...]:
    return tuple(WorkReference.from_mapping(_mapping(item, field_name)) for item in _sequence(value, field_name))


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"stored {field_name} must be an object")
    return {str(key): cast(object, item) for key, item in value.items()}


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"stored {field_name} must be an integer")
    return value


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"stored {field_name} must be an array")
    return tuple(value)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value
