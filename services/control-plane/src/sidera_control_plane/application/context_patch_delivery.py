from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPackageStatus,
    ContextPurpose,
)
from sidera_control_plane.domain.context_patches import (
    ContextPatchAcknowledgement,
    ContextPatchDeliveryResult,
    ContextPatchMaterializationV1,
    ContextPatchV1,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution import ExecutionSessionStatus
from sidera_control_plane.domain.gateway import context_patch_operation_id
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransition,
    WorkerTransitionStatus,
)
from sidera_control_plane.domain.plans import (
    TaskPlanSnapshot,
    WorkerItemPlanItem,
)


class ContextPatchGateway(Protocol):
    def apply_context_patch(
        self,
        *,
        workspace_id: UUID,
        execution_id: UUID,
        session_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        worker_transition_id: UUID,
        action_step: str,
        logical_attempt: int,
        materialization: ContextPatchMaterializationV1,
    ) -> ContextPatchDeliveryResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ContextPatchRunResult:
    outcome: Literal["converged", "deferred", "unknown"]
    patch_id: UUID | None = None
    patch_hash: str | None = None
    operation_id: UUID | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedPatch:
    transition_id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    lease_id: UUID
    fencing_token: int
    logical_attempt: int
    transition_version: int
    materialization: ContextPatchMaterializationV1


class _PatchBlocked(Exception):
    def __init__(self, code: str, *, unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.unknown = unknown


class ContextPatchDeliveryService:
    """Materialize and deliver one exact WorkerTransition ContextPatch."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        gateway: ContextPatchGateway,
        clock: Callable[[], datetime] | None = None,
        patch_ttl_seconds: int = 900,
        backoff_max_seconds: int = 60,
    ) -> None:
        if patch_ttl_seconds < 1 or backoff_max_seconds < 1:
            raise ValueError("context patch timing limits must be positive")
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._clock = clock or (lambda: datetime.now(UTC))
        self._patch_ttl_seconds = patch_ttl_seconds
        self._backoff_max_seconds = backoff_max_seconds

    def deliver(
        self,
        *,
        transition_id: UUID,
        workspace_id: UUID,
        expected_version: int,
        lease_id: UUID,
        fencing_token: int,
    ) -> ContextPatchRunResult:
        try:
            prepared = self._prepare(
                transition_id=transition_id,
                workspace_id=workspace_id,
                expected_version=expected_version,
                lease_id=lease_id,
                fencing_token=fencing_token,
                now=self._clock(),
            )
        except _PatchBlocked as exc:
            if exc.unknown:
                self._mark_unknown(
                    transition_id,
                    workspace_id,
                    lease_id,
                    fencing_token,
                    exc.code,
                    expected_version=expected_version,
                )
                return ContextPatchRunResult("unknown", error_code=exc.code)
            self._defer(
                transition_id,
                workspace_id,
                lease_id,
                fencing_token,
                exc.code,
                now=self._clock(),
                expected_version=expected_version,
            )
            return ContextPatchRunResult("deferred", error_code=exc.code)
        except Exception:  # noqa: BLE001
            code = "context_patch_prepare_unknown"
            self._mark_unknown(
                transition_id,
                workspace_id,
                lease_id,
                fencing_token,
                code,
                expected_version=expected_version,
            )
            return ContextPatchRunResult("unknown", error_code=code)

        if prepared is None:
            return ContextPatchRunResult("converged")

        patch = prepared.materialization.patch
        operation_id = _patch_operation_id(prepared)
        try:
            delivery = self._gateway.apply_context_patch(
                workspace_id=prepared.workspace_id,
                execution_id=prepared.execution_id,
                session_id=prepared.session_id,
                lease_id=prepared.lease_id,
                fencing_token=prepared.fencing_token,
                worker_transition_id=prepared.transition_id,
                action_step="apply_context_patch",
                logical_attempt=prepared.logical_attempt,
                materialization=prepared.materialization,
            )
        except Exception:  # noqa: BLE001
            code = "context_patch_gateway_unknown"
            self._mark_unknown(
                prepared.transition_id,
                prepared.workspace_id,
                prepared.lease_id,
                prepared.fencing_token,
                code,
                expected_version=prepared.transition_version,
            )
            return ContextPatchRunResult(
                "unknown",
                patch_id=patch.id,
                patch_hash=patch.patch_hash,
                operation_id=operation_id,
                error_code=code,
            )

        try:
            return self._finish(prepared, delivery, operation_id)
        except Exception:  # noqa: BLE001
            code = "context_patch_commit_unknown"
            self._mark_unknown(
                prepared.transition_id,
                prepared.workspace_id,
                prepared.lease_id,
                prepared.fencing_token,
                code,
                expected_version=prepared.transition_version,
            )
            return ContextPatchRunResult(
                "unknown",
                patch_id=patch.id,
                patch_hash=patch.patch_hash,
                operation_id=operation_id,
                error_code=code,
            )

    def _prepare(
        self,
        *,
        transition_id: UUID,
        workspace_id: UUID,
        expected_version: int,
        lease_id: UUID,
        fencing_token: int,
        now: datetime,
    ) -> _PreparedPatch | None:
        with self._uow_factory() as uow:
            transition = uow.worker_transitions.get(
                transition_id, workspace_id, for_update=True
            )
            if transition is None:
                raise _PatchBlocked("worker_transition_not_found")
            if transition.status is WorkerTransitionStatus.CONVERGED:
                return None
            if transition.status is WorkerTransitionStatus.BLOCKED_UNKNOWN:
                raise _PatchBlocked("worker_transition_unknown", unknown=True)
            if (
                transition.version != expected_version
                or transition.lease_id != lease_id
                or transition.fencing_token != fencing_token
            ):
                raise _PatchBlocked("worker_transition_fence_changed")
            if transition.status is not WorkerTransitionStatus.WAITING_SAFE_BOUNDARY:
                raise _PatchBlocked(
                    "worker_transition_not_at_safe_boundary",
                    unknown=transition.status
                    in {
                        WorkerTransitionStatus.APPLYING,
                        WorkerTransitionStatus.WAITING_ACTIVATION,
                    },
                )
            if transition.action is not WorkerTransitionAction.PATCH:
                raise _PatchBlocked("worker_transition_action_mismatch", unknown=True)
            if transition.source_execution_id is None:
                raise _PatchBlocked("patch_source_execution_missing")

            plan = uow.plan_transitions.get(
                transition.plan_transition_id, workspace_id
            )
            base_snapshot = (
                uow.plans.get(plan.from_snapshot_id, workspace_id)
                if plan is not None
                else None
            )
            target_snapshot = (
                uow.plans.get(plan.to_snapshot_id, workspace_id)
                if plan is not None
                else None
            )
            if plan is None or base_snapshot is None or target_snapshot is None:
                raise _PatchBlocked("patch_plan_snapshot_missing", unknown=True)
            if (
                base_snapshot.id != plan.from_snapshot_id
                or target_snapshot.id != plan.to_snapshot_id
                or target_snapshot.generation != plan.to_generation
            ):
                raise _PatchBlocked("patch_plan_generation_mismatch", unknown=True)

            worker_item = uow.worker_items.get(
                transition.worker_item_id, workspace_id, for_update=True
            )
            if worker_item is None:
                raise _PatchBlocked("patch_worker_item_missing", unknown=True)
            if worker_item.context_version != transition.target_context_version:
                raise _PatchBlocked("patch_target_context_version_mismatch", unknown=True)
            if worker_item.acknowledged_context_version >= transition.target_context_version:
                raise _PatchBlocked("patch_acknowledgement_missing", unknown=True)

            execution = uow.executions.get(
                transition.source_execution_id, workspace_id, for_update=True
            )
            if execution is None or execution.worker_item_id != worker_item.id:
                raise _PatchBlocked("patch_source_execution_mismatch", unknown=True)
            if execution.active_lease_id != lease_id:
                raise _PatchBlocked("patch_lease_mismatch")
            lease = uow.leases.get(lease_id, workspace_id, for_update=True)
            if (
                lease is None
                or lease.status is not LeaseStatus.ACTIVE
                or now >= lease.expires_at
                or lease.fencing_token != fencing_token
            ):
                raise _PatchBlocked("patch_lease_unavailable")

            session = uow.sessions.get_active_for_execution(
                transition.source_execution_id, for_update=True
            )
            if session is None or session.status is not ExecutionSessionStatus.ACTIVE:
                raise _PatchBlocked("active_session_unavailable")
            if transition.source_session_id not in {None, session.id}:
                raise _PatchBlocked("patch_source_session_mismatch", unknown=True)

            binding = uow.bindings.get_for_execution(
                execution.id, workspace_id, for_update=True
            )
            if (
                binding is None
                or binding.lease_id != lease_id
                or binding.fencing_token != fencing_token
            ):
                raise _PatchBlocked("patch_binding_mismatch", unknown=True)
            if 1 not in binding.capability_snapshot.context_patch_versions:
                raise _PatchBlocked("context_patch_unsupported")

            package = uow.context_packages.get_for_consumer(
                ContextPurpose.EXECUTION,
                execution.id,
                workspace_id,
                for_update=True,
            )
            if package is None:
                raise _PatchBlocked("base_context_package_missing")
            if not isinstance(package, ContextPackageManifestV1):
                raise _PatchBlocked("base_context_package_invalid", unknown=True)
            if (
                package.status is not ContextPackageStatus.ACTIVE
                or package.worker_item_id != worker_item.id
                or package.context_version is None
                or package.context_version >= transition.target_context_version
                or now >= package.expires_at
            ):
                raise _PatchBlocked("base_context_package_unavailable")
            package_ack = uow.context_packages.get_ack(
                package.package_id,
                ContextConsumerType.WORKER,
                execution.id,
                workspace_id,
                for_update=True,
            )
            if (
                package_ack is None
                or package_ack.package_id != package.package_id
                or package_ack.package_hash != package.package_hash
                or package_ack.consumer_id != execution.id
                or package_ack.context_version != package.context_version
                or package_ack.adapter_module_id != binding.adapter_module_id
                or package_ack.adapter_config_version != binding.adapter_config_version
            ):
                raise _PatchBlocked("base_context_package_ack_missing")
            base_revision = uow.work_contexts.get_revision(
                package.work_context_revision_id,
                workspace_id,
                for_update=True,
            )
            if base_revision is None:
                raise _PatchBlocked("base_context_revision_missing", unknown=True)

            patch = _build_patch(
                transition=transition,
                plan=plan,
                base_snapshot=base_snapshot,
                target_snapshot=target_snapshot,
                package=package,
                base_context_hash=base_revision.content_hash,
                now=now,
                ttl_seconds=self._patch_ttl_seconds,
            )
            stored = uow.context_patches.get(patch.id, workspace_id)
            if stored is None:
                uow.context_patches.add(patch)
            elif stored.patch_hash != patch.patch_hash or stored.to_mapping() != patch.to_mapping():
                raise _PatchBlocked("context_patch_record_conflict", unknown=True)
            else:
                patch = stored

            patch_ack = uow.context_patches.get_acknowledgement(
                patch.id, workspace_id
            )
            if patch_ack is not None:
                if (
                    patch_ack.patch_hash != patch.patch_hash
                    or patch_ack.execution_id != execution.id
                    or patch_ack.session_id != session.id
                ):
                    raise _PatchBlocked("context_patch_ack_conflict", unknown=True)
                self._converge_acknowledged(
                    uow,
                    transition,
                    worker_item,
                    patch,
                    now=now,
                )
                return None

            previous_version = transition.version
            if transition.source_session_id is None:
                transition.source_session_id = session.id
                transition.version += 1
            transition.begin_apply(now=now)
            uow.worker_transitions.save(transition, previous_version)
            return _PreparedPatch(
                transition_id=transition.id,
                workspace_id=workspace_id,
                execution_id=execution.id,
                session_id=session.id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                logical_attempt=transition.attempt_count,
                transition_version=transition.version,
                materialization=ContextPatchMaterializationV1(
                    schema_version=1,
                    patch=patch,
                ),
            )

    def _finish(
        self,
        prepared: _PreparedPatch,
        delivery: ContextPatchDeliveryResult,
        operation_id: UUID,
    ) -> ContextPatchRunResult:
        patch = prepared.materialization.patch
        if not isinstance(delivery, ContextPatchDeliveryResult):
            raise ValidationError("context patch gateway result is invalid")
        if delivery.operation_id != operation_id:
            return self._unknown_result(prepared, "patch_operation_id_mismatch")
        if delivery.patch_hash != patch.patch_hash:
            return self._unknown_result(prepared, "patch_hash_mismatch")
        if delivery.outcome == "unknown":
            return self._unknown_result(
                prepared, delivery.error_code or "context_patch_unknown"
            )
        if delivery.outcome in {"blocked", "failed"}:
            code = delivery.error_code or delivery.outcome
            self._defer(
                prepared.transition_id,
                prepared.workspace_id,
                prepared.lease_id,
                prepared.fencing_token,
                code,
                now=self._clock(),
                expected_version=prepared.transition_version,
            )
            return ContextPatchRunResult(
                "deferred",
                patch_id=patch.id,
                patch_hash=patch.patch_hash,
                operation_id=operation_id,
                error_code=code,
            )
        if delivery.accepted_sequence is None:
            return self._unknown_result(prepared, "patch_sequence_missing")

        now = self._clock()
        with self._uow_factory() as uow:
            transition = uow.worker_transitions.get(
                prepared.transition_id,
                prepared.workspace_id,
                for_update=True,
            )
            if transition is None:
                raise ValidationError("patch transition disappeared")
            if (
                transition.status is not WorkerTransitionStatus.APPLYING
                or transition.version != prepared.transition_version
                or transition.lease_id != prepared.lease_id
                or transition.fencing_token != prepared.fencing_token
            ):
                raise ValidationError("patch transition fence changed after delivery")
            stored_patch = uow.context_patches.get(
                patch.id, prepared.workspace_id, for_update=True
            )
            if stored_patch is None or stored_patch.patch_hash != patch.patch_hash:
                raise ValidationError("patch record disappeared after delivery")
            session = uow.sessions.get(
                prepared.session_id, prepared.workspace_id, for_update=True
            )
            if session is None or session.status is not ExecutionSessionStatus.ACTIVE:
                raise ValidationError("patch session changed after delivery")
            patch_ack = uow.context_patches.get_acknowledgement(
                patch.id, prepared.workspace_id
            )
            if patch_ack is None:
                patch_ack = ContextPatchAcknowledgement.create(
                    patch=stored_patch,
                    execution_id=prepared.execution_id,
                    session_id=prepared.session_id,
                    operation_id=operation_id,
                    accepted_sequence=delivery.accepted_sequence,
                    adapter_ref=f"session:{session.adapter_session_id}",
                    acknowledged_at=now,
                )
                uow.context_patches.add_acknowledgement(
                    patch_ack, prepared.workspace_id
                )
            elif (
                patch_ack.execution_id != prepared.execution_id
                or patch_ack.session_id != prepared.session_id
                or patch_ack.patch_hash != patch.patch_hash
                or patch_ack.operation_id != operation_id
                or patch_ack.accepted_sequence != delivery.accepted_sequence
            ):
                raise ValidationError("patch acknowledgement conflicts after delivery")
            worker_item = uow.worker_items.get(
                patch.worker_item_id, prepared.workspace_id, for_update=True
            )
            if worker_item is None:
                raise ValidationError("patch WorkerItem disappeared after delivery")
            previous_item_version = worker_item.version
            worker_item.acknowledge_context(
                expected_context_version=patch.target_context_version,
                now=now,
            )
            if worker_item.version != previous_item_version:
                uow.worker_items.save(worker_item, previous_item_version)
            previous_transition_version = transition.version
            transition.converge(now=now)
            uow.worker_transitions.save(transition, previous_transition_version)
        return ContextPatchRunResult(
            "converged",
            patch_id=patch.id,
            patch_hash=patch.patch_hash,
            operation_id=operation_id,
        )

    def _converge_acknowledged(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        worker_item,
        patch: ContextPatchV1,
        *,
        now: datetime,
    ) -> None:
        previous_item_version = worker_item.version
        worker_item.acknowledge_context(
            expected_context_version=patch.target_context_version,
            now=now,
        )
        if worker_item.version != previous_item_version:
            uow.worker_items.save(worker_item, previous_item_version)
        previous_transition_version = transition.version
        transition.converge(now=now)
        uow.worker_transitions.save(transition, previous_transition_version)

    def _unknown_result(
        self, prepared: _PreparedPatch, code: str
    ) -> ContextPatchRunResult:
        self._mark_unknown(
            prepared.transition_id,
            prepared.workspace_id,
            prepared.lease_id,
            prepared.fencing_token,
            code,
            expected_version=prepared.transition_version,
        )
        patch = prepared.materialization.patch
        return ContextPatchRunResult(
            "unknown",
            patch_id=patch.id,
            patch_hash=patch.patch_hash,
            operation_id=_patch_operation_id(prepared),
            error_code=code,
        )

    def _defer(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        failure_code: str,
        *,
        now: datetime,
        expected_version: int,
    ) -> None:
        try:
            with self._uow_factory() as uow:
                transition = uow.worker_transitions.get(
                    transition_id, workspace_id, for_update=True
                )
                if transition is None:
                    return
                if (
                    transition.status
                    not in {
                        WorkerTransitionStatus.WAITING_SAFE_BOUNDARY,
                        WorkerTransitionStatus.APPLYING,
                    }
                    or transition.lease_id != lease_id
                    or transition.fencing_token != fencing_token
                    or transition.version != expected_version
                ):
                    return
                previous_version = transition.version
                delay = min(
                    self._backoff_max_seconds,
                    2 ** min(max(transition.attempt_count, 0), 8),
                )
                transition.defer_safe_boundary(
                    failure_code=failure_code,
                    next_attempt_at=now + timedelta(seconds=delay),
                    now=now,
                )
                uow.worker_transitions.save(transition, previous_version)
        except Exception:  # noqa: BLE001
            self._mark_unknown(
                transition_id,
                workspace_id,
                lease_id,
                fencing_token,
                "context_patch_defer_unknown",
                expected_version=expected_version,
            )

    def _mark_unknown(
        self,
        transition_id: UUID,
        workspace_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        failure_code: str,
        *,
        expected_version: int,
    ) -> None:
        try:
            with self._uow_factory() as uow:
                transition = uow.worker_transitions.get(
                    transition_id, workspace_id, for_update=True
                )
                if transition is None:
                    return
                if (
                    transition.status
                    not in {
                        WorkerTransitionStatus.WAITING_SAFE_BOUNDARY,
                        WorkerTransitionStatus.APPLYING,
                    }
                    or transition.lease_id != lease_id
                    or transition.fencing_token != fencing_token
                    or transition.version != expected_version
                ):
                    return
                previous_version = transition.version
                transition.mark_unknown(
                    failure_code=failure_code,
                    now=self._clock(),
                )
                uow.worker_transitions.save(transition, previous_version)
        except Exception:  # noqa: BLE001
            return


def _patch_operation_id(prepared: _PreparedPatch) -> UUID:
    return context_patch_operation_id(
        prepared.transition_id,
        "apply_context_patch",
        logical_attempt=prepared.logical_attempt,
    )


def _build_patch(
    *,
    transition: WorkerTransition,
    plan,
    base_snapshot: TaskPlanSnapshot,
    target_snapshot: TaskPlanSnapshot,
    package: ContextPackageManifestV1,
    base_context_hash: str,
    now: datetime,
    ttl_seconds: int,
) -> ContextPatchV1:
    target_worker = _worker_plan(target_snapshot, transition.worker_item_id)
    base_worker = _worker_plan(base_snapshot, transition.worker_item_id)
    if target_worker is None:
        raise _PatchBlocked("patch_target_worker_missing", unknown=True)
    target_subtask = _subtask(target_snapshot, target_worker.subtask_key)
    base_subtask = (
        _subtask(base_snapshot, base_worker.subtask_key)
        if base_worker is not None
        else None
    )
    changed_requirements = (
        tuple(sorted(target_worker.capability_requirements))
        if base_worker is None
        or target_worker.capability_requirements != base_worker.capability_requirements
        else ()
    )
    changed_acceptance_refs = (
        tuple(sorted(target_subtask.acceptance_refs))
        if target_subtask is not None
        and (base_subtask is None or target_subtask.acceptance_refs != base_subtask.acceptance_refs)
        else ()
    )
    changed_dependency_refs = tuple(
        sorted(
            f"{item.upstream_worker_item_key}->{item.downstream_worker_item_key}:{item.kind}"
            for item in target_snapshot.worker_item_dependencies
            if (
                item.upstream_worker_item_key == target_worker.key
                or item.downstream_worker_item_key == target_worker.key
            )
        )
    )
    artifact_refs = tuple(
        sorted(f"{item.ref_type}:{item.ref_id}" for item in package.artifact_refs)
    )
    evidence_refs = tuple(
        sorted(f"{item.ref_type}:{item.ref_id}" for item in package.evidence_refs)
    )
    patch_id = uuid5(
        NAMESPACE_URL,
        f"sidera.context-patch:{transition.id}:v{transition.target_context_version}",
    )
    expires_at = min(package.expires_at, now + timedelta(seconds=ttl_seconds))
    if expires_at <= now:
        raise _PatchBlocked("context_patch_expired")
    return ContextPatchV1.create(
        id=patch_id,
        workspace_id=transition.workspace_id,
        root_task_id=transition.root_task_id,
        worker_item_id=transition.worker_item_id,
        plan_transition_id=transition.plan_transition_id,
        worker_transition_id=transition.id,
        base_plan_snapshot_id=base_snapshot.id,
        base_generation=base_snapshot.generation,
        target_plan_snapshot_id=target_snapshot.id,
        target_generation=target_snapshot.generation,
        base_context_package_id=package.package_id,
        base_package_hash=package.package_hash,
        base_work_context_revision_id=package.work_context_revision_id,
        base_context_hash=base_context_hash,
        target_context_version=transition.target_context_version,
        changed_requirements=changed_requirements,
        changed_acceptance_refs=changed_acceptance_refs,
        changed_dependency_refs=changed_dependency_refs,
        decision_refs=(),
        artifact_refs=artifact_refs,
        evidence_refs=evidence_refs,
        intervention_refs=(),
        unfinished_work=target_worker.responsibility,
        checkpoint_ref=(
            f"worker-item://{transition.worker_item_id}/context/"
            f"{transition.target_context_version}"
        ),
        proposer=f"plan-transition:{plan.id}",
        expires_at=expires_at,
        created_at=now,
    )


def _worker_plan(
    snapshot: TaskPlanSnapshot, worker_item_id: UUID
) -> WorkerItemPlanItem | None:
    return next(
        (
            item
            for item in snapshot.worker_item_plans
            if item.worker_item_id == worker_item_id
        ),
        None,
    )


def _subtask(snapshot: TaskPlanSnapshot, key: str):
    return next((item for item in snapshot.subtasks if item.key == key), None)
