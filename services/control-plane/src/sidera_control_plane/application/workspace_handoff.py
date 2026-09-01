from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import UUID

from sidera_control_plane.application.ports import (
    ControlPlaneUnitOfWork,
    WorkspaceProvider,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.gateway import handoff_step_operation_id
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffWorkspaceInspection,
    HandoffWorkspaceTargetSpec,
    HandoffWorkspaceTransfer,
    HandoffWorkspaceTransferStatus,
)
from sidera_control_plane.domain.handoffs import HandoffPhase, HandoffTransition
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
    workspace_location_ref_hash,
    workspace_manifest_digest,
    workspace_repository_state_digest,
)


class WorkspaceHandoffError(DomainError):
    code = "workspace_handoff_error"


class WorkspaceHandoffUnknown(WorkspaceHandoffError):
    code = "workspace_handoff_unknown"


class WorkspaceHandoffService:
    """Coordinate the provider boundary for one exact workspace handoff.

    The optional unit-of-work factory is deliberately used in short sections:
    reserve or record control-plane facts, call the provider, then persist the
    observed result. No database transaction spans an adapter call.
    """

    def __init__(
        self,
        provider: WorkspaceProvider,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork] | None = None,
    ) -> None:
        self._provider = provider
        self._uow_factory = uow_factory
        self._transfers: dict[UUID, HandoffWorkspaceTransfer] = {}
        self._transfer_workspace_by_operation: dict[UUID, UUID] = {}
        self._target_manifests: dict[UUID, ExecutionWorkspaceManifest] = {}

    def freeze_source(
        self,
        *,
        transition: HandoffTransition,
        source_manifest: ExecutionWorkspaceManifest,
        now: datetime,
    ) -> ExecutionWorkspaceManifest:
        """Freeze one exactly stopped source using a stable phase operation."""

        _aware(now, "workspace freeze timestamp")
        operation_id = handoff_step_operation_id(
            transition.id,
            HandoffPhase.FREEZE_SOURCE_WORKSPACE,
            logical_attempt=transition.logical_attempt,
            action="freeze_workspace",
        )
        self._validate_source(transition, source_manifest)
        self._validate_exact_stop(transition, source_manifest)

        existing_operation = transition.source_workspace_freeze_operation_id
        if existing_operation not in (None, operation_id):
            raise WorkspaceHandoffError("workspace freeze operation identity changed")

        if transition.source_workspace_freeze_facts is not None:
            if existing_operation != operation_id:
                raise WorkspaceHandoffError("persisted workspace freeze identity is invalid")
            self._validate_frozen_manifest(source_manifest, transition)
            return source_manifest

        if existing_operation is not None:
            observed = self._inspect_source(source_manifest)
            if observed.lifecycle is WorkspaceLifecycle.FROZEN:
                self._validate_frozen_manifest(observed, transition)
                try:
                    self._record_freeze(
                        transition,
                        observed,
                        operation_id=operation_id,
                        now=now,
                    )
                except Exception as exc:
                    raise WorkspaceHandoffUnknown(
                        "workspace freeze facts could not be persisted"
                    ) from exc
                return observed
            raise WorkspaceHandoffUnknown(
                "workspace freeze was reserved but provider state is not frozen"
            )

        self._reserve_freeze(transition, operation_id)
        if source_manifest.lifecycle is WorkspaceLifecycle.FROZEN:
            observed = self._inspect_source(source_manifest)
            if observed.lifecycle is not WorkspaceLifecycle.FROZEN:
                raise WorkspaceHandoffUnknown(
                    "workspace manifest is frozen but provider state is not frozen"
                )
        else:
            try:
                observed = self._provider.freeze(
                    source_manifest,
                    operation_id=operation_id,
                )
            except Exception as exc:
                if not _is_unknown(exc):
                    raise WorkspaceHandoffError("workspace freeze failed") from exc
                try:
                    observed = self._inspect_source(source_manifest)
                except Exception as inspect_exc:
                    raise WorkspaceHandoffUnknown(
                        "workspace freeze result is unknown"
                    ) from inspect_exc
                if observed.lifecycle is not WorkspaceLifecycle.FROZEN:
                    raise WorkspaceHandoffUnknown(
                        "workspace freeze result is not confirmed"
                    )

        self._validate_frozen_manifest(observed, transition)
        try:
            self._record_freeze(
                transition,
                observed,
                operation_id=operation_id,
                now=now,
            )
        except Exception as exc:
            raise WorkspaceHandoffUnknown(
                "workspace freeze facts could not be persisted"
            ) from exc
        return observed

    def _validate_source(
        self,
        transition: HandoffTransition,
        source_manifest: ExecutionWorkspaceManifest,
    ) -> None:
        if not isinstance(source_manifest, ExecutionWorkspaceManifest):
            raise WorkspaceHandoffError("source workspace manifest is invalid")
        if source_manifest.workspace_id != transition.workspace_id:
            raise WorkspaceHandoffError("workspace owner does not match transition")
        if source_manifest.execution_id != transition.source_execution_id:
            raise WorkspaceHandoffError("source execution owner does not match transition")
        if source_manifest.id != transition.source_workspace_manifest_id:
            raise WorkspaceHandoffError("source workspace manifest identity changed")
        if source_manifest.lifecycle not in {
            WorkspaceLifecycle.ACTIVE,
            WorkspaceLifecycle.FROZEN,
        }:
            raise WorkspaceHandoffError("source workspace is not handoffable")

    def _validate_exact_stop(
        self,
        transition: HandoffTransition,
        source_manifest: ExecutionWorkspaceManifest,
    ) -> None:
        if transition.source_checkpoint_sequence is None:
            raise WorkspaceHandoffError("workspace freeze requires a source checkpoint")
        if transition.source_stop_sequence != transition.source_checkpoint_sequence:
            raise WorkspaceHandoffError("workspace freeze requires an exact source stop")
        checkpoint = transition.source_checkpoint_facts
        stop = transition.source_stop_facts
        if checkpoint is None or stop is None:
            raise WorkspaceHandoffError("source checkpoint and stop facts are required")
        if _uuid_value(checkpoint, "transition_id") != transition.id:
            raise WorkspaceHandoffError("checkpoint transition identity changed")
        if _uuid_value(checkpoint, "source_session_id") != transition.source_session_id:
            raise WorkspaceHandoffError("checkpoint Session identity changed")
        if _int_value(checkpoint, "accepted_sequence") != transition.source_checkpoint_sequence:
            raise WorkspaceHandoffError("checkpoint sequence changed")
        if checkpoint.get("quiescent") is not True:
            raise WorkspaceHandoffError("checkpoint did not prove quiescence")
        if checkpoint.get("workspace_digest") != workspace_manifest_digest(source_manifest):
            raise WorkspaceHandoffError("checkpoint workspace digest changed")
        if _uuid_value(stop, "source_session_id") != transition.source_session_id:
            raise WorkspaceHandoffError("stop Session identity changed")
        if _int_value(stop, "accepted_sequence") != transition.source_stop_sequence:
            raise WorkspaceHandoffError("stop sequence changed")
        if stop.get("stopped") is not True:
            raise WorkspaceHandoffError("stop facts do not prove stopped=true")

    def _validate_frozen_manifest(
        self,
        manifest: ExecutionWorkspaceManifest,
        transition: HandoffTransition,
    ) -> None:
        self._validate_source(transition, manifest)
        if manifest.lifecycle is not WorkspaceLifecycle.FROZEN:
            raise WorkspaceHandoffError("provider did not return a frozen source")
        self._validate_exact_stop(transition, manifest)

    def _inspect_source(
        self, source_manifest: ExecutionWorkspaceManifest
    ) -> ExecutionWorkspaceManifest:
        exact_inspector = getattr(self._provider, "inspect_manifest", None)
        if callable(exact_inspector):
            result = exact_inspector(source_manifest)
        else:
            result = self._provider.inspect(source_manifest.location_ref)
        if not isinstance(result, ExecutionWorkspaceManifest):
            raise WorkspaceHandoffError("provider returned an invalid workspace inspection")
        return result

    def _reserve_freeze(self, transition: HandoffTransition, operation_id: UUID) -> None:
        if self._uow_factory is None:
            transition.reserve_workspace_freeze_operation(operation_id=operation_id)
            return
        with self._uow_factory() as uow:
            current = uow.handoff_transitions.get(
                transition.id,
                transition.workspace_id,
                for_update=True,
            )
            if current is None:
                raise WorkspaceHandoffError("handoff transition not found")
            previous_version = current.version
            current.reserve_workspace_freeze_operation(operation_id=operation_id)
            if current.version != previous_version:
                uow.handoff_transitions.save(current, previous_version)
            _sync_freeze_state(transition, current)

    def _record_freeze(
        self,
        transition: HandoffTransition,
        manifest: ExecutionWorkspaceManifest,
        *,
        operation_id: UUID,
        now: datetime,
    ) -> None:
        facts = {
            "operation_id": str(operation_id),
            "transition_id": str(transition.id),
            "manifest_id": str(manifest.id),
            "workspace_id": str(manifest.workspace_id),
            "execution_id": str(manifest.execution_id),
            "fencing_token": manifest.fencing_token,
            "workspace_digest": workspace_manifest_digest(manifest),
            "location_ref": manifest.location_ref,
            "lifecycle": manifest.lifecycle.value,
            "stop_sequence": transition.source_stop_sequence,
            "frozen_at": (manifest.frozen_at or now).isoformat(),
        }
        if self._uow_factory is None:
            transition.record_workspace_freeze(
                operation_id=operation_id,
                facts=facts,
            )
            return
        with self._uow_factory() as uow:
            current = uow.handoff_transitions.get(
                transition.id,
                transition.workspace_id,
                for_update=True,
            )
            if current is None:
                raise WorkspaceHandoffError("handoff transition not found")
            stored_manifest = uow.workspaces.get_for_execution(
                manifest.execution_id,
                manifest.workspace_id,
                for_update=True,
            )
            if stored_manifest is None or stored_manifest.id != manifest.id:
                raise WorkspaceHandoffError("source workspace manifest not found")
            if not _same_manifest(stored_manifest, manifest):
                uow.workspaces.save(manifest, stored_manifest.version)
            if current.source_workspace_freeze_facts is None:
                previous_version = current.version
                current.record_workspace_freeze(
                    operation_id=operation_id,
                    facts=facts,
                )
                uow.handoff_transitions.save(current, previous_version)
            elif dict(current.source_workspace_freeze_facts) != facts:
                raise WorkspaceHandoffError("workspace freeze facts conflict")
            _sync_freeze_state(transition, current)

    def transfer(
        self,
        *,
        transition: HandoffTransition,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
        now: datetime,
    ) -> HandoffWorkspaceTransfer:
        """Perform or reuse one exact pre-Lease workspace transfer."""

        _aware(now, "workspace transfer timestamp")
        self._validate_source(transition, source_manifest)
        self._validate_exact_stop(transition, source_manifest)
        if source_manifest.lifecycle is not WorkspaceLifecycle.FROZEN:
            raise WorkspaceHandoffError("workspace transfer requires a frozen source")
        if transition.source_workspace_freeze_operation_id is None:
            raise WorkspaceHandoffError("workspace transfer requires source freeze facts")
        self._validate_target(source_manifest, transition, target)
        operation_id = handoff_step_operation_id(
            transition.id,
            HandoffPhase.TRANSFER_WORKSPACE,
            logical_attempt=transition.logical_attempt,
            action="transfer_workspace",
        )

        existing = self._load_transfer(operation_id, transition.workspace_id)
        if existing is not None:
            self._validate_transfer_identity(
                existing,
                transition=transition,
                source_manifest=source_manifest,
                target=target,
            )
            if existing.status is not HandoffWorkspaceTransferStatus.REQUESTED:
                if existing.status is HandoffWorkspaceTransferStatus.SUCCEEDED:
                    target_manifest = self._manifest_by_id(
                        existing.target_manifest_id,
                        transition.workspace_id,
                    )
                    if target_manifest is None:
                        raise WorkspaceHandoffUnknown(
                            "successful workspace transfer target is unavailable"
                        )
                    self._validate_target_manifest(
                        target_manifest,
                        source_manifest,
                        target,
                    )
                return existing
            return self._reconcile_requested_transfer(
                existing,
                transition=transition,
                source_manifest=source_manifest,
                target=target,
                now=now,
            )

        requested = HandoffWorkspaceTransfer(
            transition_id=transition.id,
            logical_attempt=transition.logical_attempt,
            operation_id=operation_id,
            source_manifest_id=source_manifest.id,
            target_manifest_id=None,
            source_execution_id=source_manifest.execution_id,
            target_execution_id=target.target_execution_id,
            source_fencing_token=source_manifest.fencing_token,
            target_fencing_token=target.anticipated_fencing_token,
            location_ref_hash=workspace_location_ref_hash(source_manifest.location_ref),
            repository_state_digest=workspace_repository_state_digest(source_manifest),
            status=HandoffWorkspaceTransferStatus.REQUESTED,
            failure_code=None,
            created_at=now,
            updated_at=now,
        )
        self._persist_requested(requested, transition.workspace_id)
        try:
            target_manifest = self._provider.transfer_handoff(
                source_manifest,
                target,
                transition_id=transition.id,
                logical_attempt=transition.logical_attempt,
                operation_id=operation_id,
            )
        except Exception as exc:
            status = (
                HandoffWorkspaceTransferStatus.UNKNOWN
                if _is_unknown(exc)
                else HandoffWorkspaceTransferStatus.FAILED
            )
            result = _transfer_outcome(
                requested,
                status=status,
                failure_code=_failure_code(exc),
                now=now,
            )
            try:
                self._persist_outcome(
                    result,
                    target_manifest=None,
                    workspace_id=transition.workspace_id,
                )
            except Exception as persist_exc:
                raise WorkspaceHandoffUnknown(
                    "workspace transfer result could not be persisted"
                ) from persist_exc
            if status is HandoffWorkspaceTransferStatus.UNKNOWN:
                return result
            raise WorkspaceHandoffError("workspace transfer failed") from exc

        try:
            self._validate_target_manifest(target_manifest, source_manifest, target)
        except Exception as exc:
            result = _transfer_outcome(
                requested,
                status=HandoffWorkspaceTransferStatus.FAILED,
                failure_code=_failure_code(exc),
                now=now,
            )
            try:
                self._persist_outcome(
                    result,
                    target_manifest=None,
                    workspace_id=transition.workspace_id,
                )
            except Exception as persist_exc:
                raise WorkspaceHandoffUnknown(
                    "invalid workspace transfer result could not be persisted"
                ) from persist_exc
            raise WorkspaceHandoffError("workspace transfer result is invalid") from exc

        result = _transfer_outcome(
            requested,
            status=HandoffWorkspaceTransferStatus.SUCCEEDED,
            target_manifest_id=target_manifest.id,
            failure_code=None,
            now=now,
        )
        try:
            self._persist_outcome(
                result,
                target_manifest=target_manifest,
                workspace_id=transition.workspace_id,
            )
        except Exception as exc:
            persisted = self._load_transfer(operation_id, transition.workspace_id)
            if persisted is not None and persisted.status is not HandoffWorkspaceTransferStatus.REQUESTED:
                return persisted
            unknown = _transfer_outcome(
                persisted or requested,
                status=HandoffWorkspaceTransferStatus.UNKNOWN,
                failure_code="transfer_persistence_unknown",
                now=now,
            )
            try:
                self._persist_outcome(
                    unknown,
                    target_manifest=None,
                    workspace_id=transition.workspace_id,
                )
            except Exception as persist_exc:
                refreshed = self._load_transfer(operation_id, transition.workspace_id)
                if (
                    refreshed is not None
                    and refreshed.status is not HandoffWorkspaceTransferStatus.REQUESTED
                ):
                    return refreshed
                raise WorkspaceHandoffUnknown(
                    "workspace transfer persistence result is unknown"
                ) from persist_exc
            raise WorkspaceHandoffUnknown(
                "workspace transfer succeeded but its result was not persisted"
            ) from exc
        self._target_manifests[target_manifest.id] = target_manifest
        return result

    def inspect(
        self,
        *,
        transition_id: UUID,
        workspace_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
        now: datetime,
    ) -> HandoffWorkspaceInspection:
        """Inspect one transfer identity without replaying it."""

        _aware(now, "workspace inspection timestamp")
        if logical_attempt < 1:
            raise WorkspaceHandoffError("logical_attempt must be positive")
        existing = self._load_transfer(operation_id, workspace_id)
        if existing is not None:
            if (
                existing.transition_id != transition_id
                or existing.logical_attempt != logical_attempt
            ):
                raise WorkspaceHandoffError("workspace inspection identity changed")
            if existing.target_manifest_id is not None:
                self._manifest_by_id(existing.target_manifest_id, workspace_id)
            return _inspection_for_transfer(existing, now, self._target_manifests)

        try:
            inspection = self._provider.inspect_handoff_transfer(
                transition_id=transition_id,
                logical_attempt=logical_attempt,
                operation_id=operation_id,
            )
        except Exception as exc:
            raise WorkspaceHandoffUnknown(
                "workspace transfer inspection is unavailable"
            ) from exc
        _validate_inspection_identity(
            inspection,
            transition_id=transition_id,
            logical_attempt=logical_attempt,
            operation_id=operation_id,
        )
        return inspection

    def _validate_target(
        self,
        source_manifest: ExecutionWorkspaceManifest,
        transition: HandoffTransition,
        target: HandoffWorkspaceTargetSpec,
    ) -> None:
        if target.workspace_id != source_manifest.workspace_id:
            raise WorkspaceHandoffError("target workspace owner does not match source")
        if target.task_id != source_manifest.task_id:
            raise WorkspaceHandoffError("target task owner does not match source")
        if target.target_execution_id == source_manifest.execution_id:
            raise WorkspaceHandoffError("target execution must be new")
        if target.target_worker_id != transition.target_worker_id:
            raise WorkspaceHandoffError("target worker does not match transition")
        if target.anticipated_fencing_token != source_manifest.fencing_token + 1:
            raise WorkspaceHandoffError("target fencing token must increment exactly")
        self._validate_repositories(source_manifest, target.repositories)
        validator = getattr(self._provider, "validate_handoff_target", None)
        if callable(validator):
            validator(source_manifest, target)

    @staticmethod
    def _validate_repositories(
        source_manifest: ExecutionWorkspaceManifest,
        repositories: tuple[Mapping[str, object], ...],
    ) -> None:
        if len(repositories) != len(source_manifest.checkouts):
            raise WorkspaceHandoffError("target checkout count does not match source")
        for checkout, target_repository in zip(
            source_manifest.checkouts, repositories, strict=True
        ):
            repository_ref = _optional_text(target_repository, "repository_ref")
            if repository_ref is None:
                raise WorkspaceHandoffError("target repository reference is required")
            if repository_ref != checkout.repository_ref:
                raise WorkspaceHandoffError("target repository does not match source")
            revision = _optional_text(target_repository, "base_revision")
            if revision is None:
                revision = _optional_text(target_repository, "revision")
            if revision is not None and revision != checkout.base_revision:
                raise WorkspaceHandoffError("target checkout revision does not match source")
            for key, expected in (
                ("branch", checkout.branch),
                ("current_head", checkout.current_head),
            ):
                actual = _optional_text(target_repository, key)
                if actual is not None and actual != expected:
                    raise WorkspaceHandoffError(f"target {key} does not match source")

    @staticmethod
    def _validate_target_manifest(
        manifest: ExecutionWorkspaceManifest,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
    ) -> None:
        if not isinstance(manifest, ExecutionWorkspaceManifest):
            raise WorkspaceHandoffError("provider returned an invalid target manifest")
        if manifest.id == source_manifest.id:
            raise WorkspaceHandoffError("target manifest must have a new identity")
        if manifest.workspace_id != source_manifest.workspace_id:
            raise WorkspaceHandoffError("target manifest workspace owner changed")
        if manifest.task_id != target.task_id:
            raise WorkspaceHandoffError("target manifest task owner changed")
        if manifest.execution_id != target.target_execution_id:
            raise WorkspaceHandoffError("target manifest execution owner changed")
        if manifest.location_ref != source_manifest.location_ref:
            raise WorkspaceHandoffError("target location reference changed")
        if manifest.checkouts != source_manifest.checkouts:
            raise WorkspaceHandoffError("target checkout state changed")
        if manifest.fencing_token != target.anticipated_fencing_token:
            raise WorkspaceHandoffError("target fencing token changed")
        if manifest.lifecycle is not WorkspaceLifecycle.READY:
            raise WorkspaceHandoffError("workspace transfer target must be READY")

    def _validate_transfer_identity(
        self,
        transfer: HandoffWorkspaceTransfer,
        *,
        transition: HandoffTransition,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
    ) -> None:
        expected = {
            "transition_id": transition.id,
            "logical_attempt": transition.logical_attempt,
            "source_manifest_id": source_manifest.id,
            "source_execution_id": source_manifest.execution_id,
            "target_execution_id": target.target_execution_id,
            "source_fencing_token": source_manifest.fencing_token,
            "target_fencing_token": target.anticipated_fencing_token,
            "location_ref_hash": workspace_location_ref_hash(source_manifest.location_ref),
            "repository_state_digest": workspace_repository_state_digest(source_manifest),
        }
        for field, value in expected.items():
            if getattr(transfer, field) != value:
                raise WorkspaceHandoffError("workspace transfer provenance changed")

    def _reconcile_requested_transfer(
        self,
        existing: HandoffWorkspaceTransfer,
        *,
        transition: HandoffTransition,
        source_manifest: ExecutionWorkspaceManifest,
        target: HandoffWorkspaceTargetSpec,
        now: datetime,
    ) -> HandoffWorkspaceTransfer:
        inspection = self.inspect(
            transition_id=transition.id,
            workspace_id=transition.workspace_id,
            logical_attempt=transition.logical_attempt,
            operation_id=existing.operation_id,
            now=now,
        )
        if inspection.outcome is HandoffInspectionOutcome.SUCCEEDED:
            target_manifest = self._manifest_by_id(
                inspection.target_manifest_id,
                transition.workspace_id,
            )
            if target_manifest is not None:
                self._validate_target_manifest(target_manifest, source_manifest, target)
                result = _transfer_outcome(
                    existing,
                    status=HandoffWorkspaceTransferStatus.SUCCEEDED,
                    target_manifest_id=target_manifest.id,
                    failure_code=None,
                    now=now,
                )
                self._persist_outcome(
                    result,
                    target_manifest=target_manifest,
                    workspace_id=transition.workspace_id,
                )
                return result
        result = _transfer_outcome(
            existing,
            status=HandoffWorkspaceTransferStatus.UNKNOWN,
            failure_code="transfer_requires_reconciliation",
            now=now,
        )
        self._persist_outcome(
            result,
            target_manifest=None,
            workspace_id=transition.workspace_id,
        )
        return result

    def _load_transfer(
        self, operation_id: UUID, workspace_id: UUID
    ) -> HandoffWorkspaceTransfer | None:
        local = self._transfers.get(operation_id)
        if local is not None:
            if self._transfer_workspace_by_operation.get(operation_id) != workspace_id:
                raise WorkspaceHandoffError("workspace transfer owner changed")
            return local
        if self._uow_factory is None:
            return None
        with self._uow_factory() as uow:
            transfer = uow.handoff_workspace_transfers.get_by_operation(
                operation_id,
                workspace_id,
            )
        if transfer is not None:
            self._transfers[operation_id] = transfer
            self._transfer_workspace_by_operation[operation_id] = workspace_id
        return transfer

    def _persist_requested(
        self, transfer: HandoffWorkspaceTransfer, workspace_id: UUID
    ) -> None:
        if self._uow_factory is None:
            self._transfers[transfer.operation_id] = transfer
            self._transfer_workspace_by_operation[transfer.operation_id] = workspace_id
            return
        with self._uow_factory() as uow:
            existing = uow.handoff_workspace_transfers.get_by_operation(
                transfer.operation_id,
                workspace_id,
            )
            if existing is None:
                uow.handoff_workspace_transfers.add_append_only(
                    transfer,
                    workspace_id,
                )
                existing = transfer
            elif not _same_transfer_provenance(existing, transfer):
                raise WorkspaceHandoffError(
                    "workspace transfer provenance changed during persistence"
                )
            self._transfers[transfer.operation_id] = existing
            self._transfer_workspace_by_operation[transfer.operation_id] = workspace_id

    def _persist_outcome(
        self,
        transfer: HandoffWorkspaceTransfer,
        *,
        target_manifest: ExecutionWorkspaceManifest | None,
        workspace_id: UUID,
    ) -> None:
        if self._uow_factory is None:
            self._transfers[transfer.operation_id] = transfer
            self._transfer_workspace_by_operation[transfer.operation_id] = workspace_id
            if target_manifest is not None:
                self._target_manifests[target_manifest.id] = target_manifest
            return
        with self._uow_factory() as uow:
            existing = uow.handoff_workspace_transfers.get_by_operation(
                transfer.operation_id,
                workspace_id,
            )
            if existing is None:
                if target_manifest is not None:
                    uow.workspaces.add(target_manifest)
                uow.handoff_workspace_transfers.add_append_only(
                    transfer,
                    workspace_id,
                )
            else:
                if target_manifest is not None:
                    persisted_target = uow.workspaces.get_for_execution(
                        target_manifest.execution_id,
                        target_manifest.workspace_id,
                    )
                    if persisted_target is None:
                        uow.workspaces.add(target_manifest)
                    elif persisted_target.id != target_manifest.id:
                        raise WorkspaceHandoffError(
                            "target execution already has a different workspace manifest"
                        )
                if existing != transfer:
                    uow.handoff_workspace_transfers.save(
                        transfer,
                        existing.version,
                    )
            self._transfers[transfer.operation_id] = transfer
            self._transfer_workspace_by_operation[transfer.operation_id] = workspace_id
            if target_manifest is not None:
                self._target_manifests[target_manifest.id] = target_manifest

    def _manifest_by_id(
        self,
        manifest_id: UUID | None,
        workspace_id: UUID,
    ) -> ExecutionWorkspaceManifest | None:
        if manifest_id is None:
            return None
        local = self._target_manifests.get(manifest_id)
        if local is not None:
            return local
        finder = getattr(self._provider, "manifest_by_id", None)
        if callable(finder):
            result = finder(manifest_id)
            if isinstance(result, ExecutionWorkspaceManifest):
                if result.id != manifest_id or result.workspace_id != workspace_id:
                    raise WorkspaceHandoffError("workspace manifest owner changed")
                self._target_manifests[manifest_id] = result
                return result
        if self._uow_factory is not None:
            with self._uow_factory() as uow:
                finder = getattr(uow.workspaces, "get", None)
                if callable(finder):
                    result = finder(manifest_id, workspace_id)
                else:
                    result = None
            if isinstance(result, ExecutionWorkspaceManifest):
                if result.id != manifest_id or result.workspace_id != workspace_id:
                    raise WorkspaceHandoffError("workspace manifest owner changed")
                self._target_manifests[manifest_id] = result
                return result
        return None


def _transfer_outcome(
    transfer: HandoffWorkspaceTransfer,
    *,
    status: HandoffWorkspaceTransferStatus,
    failure_code: str | None,
    now: datetime,
    target_manifest_id: UUID | None = None,
) -> HandoffWorkspaceTransfer:
    return HandoffWorkspaceTransfer(
        transition_id=transfer.transition_id,
        logical_attempt=transfer.logical_attempt,
        operation_id=transfer.operation_id,
        source_manifest_id=transfer.source_manifest_id,
        target_manifest_id=target_manifest_id,
        source_execution_id=transfer.source_execution_id,
        target_execution_id=transfer.target_execution_id,
        source_fencing_token=transfer.source_fencing_token,
        target_fencing_token=transfer.target_fencing_token,
        location_ref_hash=transfer.location_ref_hash,
        repository_state_digest=transfer.repository_state_digest,
        status=status,
        failure_code=failure_code,
        created_at=transfer.created_at,
        updated_at=now,
        version=transfer.version + 1,
    )


def _same_transfer_provenance(
    left: HandoffWorkspaceTransfer,
    right: HandoffWorkspaceTransfer,
) -> bool:
    return (
        left.transition_id == right.transition_id
        and left.logical_attempt == right.logical_attempt
        and left.operation_id == right.operation_id
        and left.source_manifest_id == right.source_manifest_id
        and left.target_manifest_id == right.target_manifest_id
        and left.source_execution_id == right.source_execution_id
        and left.target_execution_id == right.target_execution_id
        and left.source_fencing_token == right.source_fencing_token
        and left.target_fencing_token == right.target_fencing_token
        and left.location_ref_hash == right.location_ref_hash
        and left.repository_state_digest == right.repository_state_digest
    )


def _inspection_for_transfer(
    transfer: HandoffWorkspaceTransfer,
    now: datetime,
    target_manifests: Mapping[UUID, ExecutionWorkspaceManifest],
) -> HandoffWorkspaceInspection:
    if transfer.status is HandoffWorkspaceTransferStatus.SUCCEEDED:
        outcome = HandoffInspectionOutcome.SUCCEEDED
    elif transfer.status is HandoffWorkspaceTransferStatus.FAILED:
        outcome = HandoffInspectionOutcome.FAILED
    elif transfer.status is HandoffWorkspaceTransferStatus.UNKNOWN:
        outcome = HandoffInspectionOutcome.UNKNOWN
    else:
        outcome = HandoffInspectionOutcome.IN_PROGRESS
    target = (
        target_manifests.get(transfer.target_manifest_id)
        if transfer.target_manifest_id is not None
        else None
    )
    return HandoffWorkspaceInspection(
        transition_id=transfer.transition_id,
        logical_attempt=transfer.logical_attempt,
        operation_id=transfer.operation_id,
        outcome=outcome,
        source_manifest_id=transfer.source_manifest_id,
        target_manifest_id=transfer.target_manifest_id,
        source_lifecycle=None,
        target_lifecycle=target.lifecycle.value if target is not None else None,
        observed_at=now,
        evidence_refs=(f"workspace-transfer:{transfer.operation_id}",),
    )


def _validate_inspection_identity(
    inspection: HandoffWorkspaceInspection,
    *,
    transition_id: UUID,
    logical_attempt: int,
    operation_id: UUID,
) -> None:
    if not isinstance(inspection, HandoffWorkspaceInspection):
        raise WorkspaceHandoffError("provider returned an invalid workspace inspection")
    if (
        inspection.transition_id != transition_id
        or inspection.logical_attempt != logical_attempt
        or inspection.operation_id != operation_id
    ):
        raise WorkspaceHandoffError("workspace inspection identity changed")


def _sync_freeze_state(target: HandoffTransition, source: HandoffTransition) -> None:
    target.source_workspace_freeze_operation_id = (
        source.source_workspace_freeze_operation_id
    )
    target.source_workspace_freeze_facts = source.source_workspace_freeze_facts
    target.version = source.version


def _same_manifest(
    left: ExecutionWorkspaceManifest,
    right: ExecutionWorkspaceManifest,
) -> bool:
    return (
        left.id == right.id
        and left.workspace_id == right.workspace_id
        and left.task_id == right.task_id
        and left.execution_id == right.execution_id
        and left.checkouts == right.checkouts
        and left.location_ref == right.location_ref
        and left.lifecycle is right.lifecycle
        and left.retention_seconds == right.retention_seconds
        and left.retain_until == right.retain_until
        and left.fencing_token == right.fencing_token
        and left.created_at == right.created_at
        and left.frozen_at == right.frozen_at
        and left.released_at == right.released_at
        and left.version == right.version
    )


def _uuid_value(facts: Mapping[str, object], name: str) -> UUID:
    value = facts.get(name)
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkspaceHandoffError(f"source fact {name} is invalid") from exc


def _int_value(facts: Mapping[str, object], name: str) -> int:
    value = facts.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkspaceHandoffError(f"source fact {name} is invalid")
    return value


def _optional_text(facts: Mapping[str, object], name: str) -> str | None:
    value = facts.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceHandoffError(f"target repository {name} is invalid")
    return value.strip()


def _failure_code(exc: Exception) -> str:
    value = getattr(exc, "code", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    text = str(exc).strip().lower().replace(" ", "_")
    return text[:100] or "workspace_provider_error"


def _is_unknown(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or bool(getattr(exc, "unknown", False)) or (
        exc.__class__.__name__.lower().endswith("unknown")
    )


def _aware(value: object, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkspaceHandoffError(f"{label} must be timezone-aware")
