from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.code_result_delivery import resolve_code_inputs
from sidera_control_plane.application.context_requests import (
    ExecutionContextAuthorizationError,
    build_derived_execution_context_assembly_request,
    build_execution_context_assembly_request,
    execution_context_manifest_schema_version,
    load_execution_context_selection,
    resolve_execution_context_authorization,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPackageManifestV3,
    ContextPackageStatus,
    ContextPurpose,
    ResultInputManifestV1,
    context_package_reference,
)
from sidera_control_plane.domain.errors import DomainError, ValidationError
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffContextBundle,
    HandoffInputAction,
    HandoffInputClassification,
    HandoffSourceFacts,
    HandoffStopEvidence,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffProposalStatus,
    HandoffTransition,
)
from sidera_control_plane.domain.input_selections import CollaborationInputSelectionV1
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.domain.work_context import WorkContextRevision
from sidera_control_plane.domain.worker_item import WorkerItem
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
    workspace_location_ref_hash,
    workspace_manifest_digest,
)

_HANDOFF_CONTEXT_NAMESPACE = NAMESPACE_URL
_SAFE_WORKSPACE_STATE_KEYS = frozenset(
    {
        "manifest_id",
        "workspace_id",
        "execution_id",
        "workspace_digest",
        "lifecycle",
        "stop_sequence",
        "frozen_at",
        "repository_state",
    }
)
_PENDING_ASSEMBLY_STATUSES = frozenset(
    {
        ContextAssemblyStatus.QUEUED,
        ContextAssemblyStatus.ASSEMBLING,
        ContextAssemblyStatus.RETRY_WAIT,
    }
)


class HandoffContextError(DomainError):
    """A handoff bundle or target context package cannot be trusted."""

    code = "handoff_context_invalid"

    def __init__(
        self,
        message: str,
        *,
        code: str = "handoff_context_invalid",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.code = code


class HandoffContextService:
    """Build immutable handoff context evidence and its target package request."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        context_ttl_seconds: int = 3600,
    ) -> None:
        if context_ttl_seconds < 1:
            raise ValueError("handoff context TTL must be positive")
        self._uow_factory = uow_factory
        self._context_ttl_seconds = context_ttl_seconds

    def collect_source_facts(
        self,
        *,
        transition: HandoffTransition,
        workspace_id: UUID,
        input_classification: HandoffInputClassification,
    ) -> HandoffSourceFacts:
        """Collect bounded source evidence after an exact stop and freeze."""

        if transition.workspace_id != workspace_id:
            raise HandoffContextError("handoff source workspace does not match")
        checkpoint = _checkpoint_from_transition(transition)
        stop = _stop_from_transition(transition)
        freeze_facts = transition.source_workspace_freeze_facts
        if freeze_facts is None:
            raise HandoffContextError(
                "handoff source workspace is not frozen",
                code="handoff_context_source_not_frozen",
            )
        if freeze_facts.get("lifecycle") != WorkspaceLifecycle.FROZEN.value:
            raise HandoffContextError(
                "handoff source workspace is not frozen",
                code="handoff_context_source_not_frozen",
            )
        if str(freeze_facts.get("manifest_id")) != str(
            transition.source_workspace_manifest_id
        ):
            raise HandoffContextError("handoff source manifest identity changed")
        if transition.handoff_schema_version != 2 and (
            freeze_facts.get("workspace_digest") != checkpoint.workspace_digest
        ):
            raise HandoffContextError("handoff source workspace digest changed")
        if transition.source_workspace_freeze_operation_id is None:
            raise HandoffContextError("handoff source workspace freeze is incomplete")
        if checkpoint.transition_id != transition.id:
            raise HandoffContextError("handoff checkpoint transition identity changed")
        if checkpoint.source_session_id != transition.source_session_id:
            raise HandoffContextError("handoff checkpoint Session identity changed")
        if stop.source_session_id != transition.source_session_id:
            raise HandoffContextError("handoff stop Session identity changed")
        stop.assert_matches(checkpoint)
        if (
            transition.handoff_schema_version != 2
            and stop.accepted_sequence != checkpoint.accepted_sequence
        ):
            raise HandoffContextError("handoff stop sequence changed")
        if transition.handoff_schema_version == 2:
            from sidera_control_plane.application.workspace_handoff import (
                WorkspaceHandoffService,
            )

            with self._uow_factory() as uow:
                manifest = uow.workspaces.get(
                    transition.source_workspace_manifest_id, transition.workspace_id
                )
                if manifest is None:
                    raise HandoffContextError("native frozen source facts changed")
                WorkspaceHandoffService._validate_exact_stop(transition, manifest)
                if freeze_facts.get("workspace_digest") != workspace_manifest_digest(
                    manifest
                ):
                    raise HandoffContextError("native frozen source facts changed")
        _validate_input_classification(transition, input_classification)
        return HandoffSourceFacts(
            checkpoint=checkpoint,
            stop=stop,
            source_context_package_ref=context_package_reference(
                checkpoint.context_package_id,
                checkpoint.context_package_hash,
            ),
            source_worker_context_version=checkpoint.context_version,
            workspace_state=_safe_workspace_state(freeze_facts),
            artifact_refs=checkpoint.artifact_refs,
            evidence_refs=checkpoint.evidence_refs,
            input_classification=input_classification,
        )

    def create_bundle_and_request(
        self,
        *,
        transition: HandoffTransition,
        proposal: HandoffProposal,
        target_request: ExecutionRequest,
        target_execution: Execution,
        source_facts: HandoffSourceFacts,
        now: datetime,
        uow: ControlPlaneUnitOfWork | None = None,
    ) -> tuple[HandoffContextBundle, ContextAssemblyRequest]:
        """Persist one immutable Bundle and enqueue one target package request."""

        _aware(now, "handoff context timestamp")
        if uow is not None:
            return self._create_in_uow(
                uow,
                transition=transition,
                proposal=proposal,
                target_request=target_request,
                target_execution=target_execution,
                source_facts=source_facts,
                now=now,
            )
        with self._uow_factory() as target_uow:
            return self._create_in_uow(
                target_uow,
                transition=transition,
                proposal=proposal,
                target_request=target_request,
                target_execution=target_execution,
                source_facts=source_facts,
                now=now,
            )

    def _create_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        transition: HandoffTransition,
        proposal: HandoffProposal,
        target_request: ExecutionRequest,
        target_execution: Execution,
        source_facts: HandoffSourceFacts,
        now: datetime,
    ) -> tuple[HandoffContextBundle, ContextAssemblyRequest]:
        current = uow.handoff_transitions.get(
            transition.id,
            transition.workspace_id,
            for_update=True,
        )
        if current is None:
            raise HandoffContextError("handoff transition was not found")
        if current.version != transition.version:
            raise HandoffContextError("handoff transition version is stale")
        if current.phase is not HandoffPhase.ASSEMBLE_BUNDLE:
            raise HandoffContextError("handoff transition is not assembling a Bundle")
        if proposal.status is not HandoffProposalStatus.CONFIRMED:
            raise HandoffContextError("handoff proposal is not confirmed")
        if current.proposal_id != proposal.id:
            raise HandoffContextError("handoff proposal identity changed")
        if current.target_execution_id != target_execution.id:
            raise HandoffContextError("handoff target execution identity changed")
        if source_facts.input_classification is None:
            raise HandoffContextError("handoff input classification is missing")
        _validate_input_classification(current, source_facts.input_classification)
        stored_execution = uow.executions.get(
            target_execution.id,
            current.workspace_id,
            for_update=True,
        )
        if stored_execution is None:
            raise HandoffContextError("handoff target execution was not found")
        stored_request = uow.execution_requests.get(
            target_request.id,
            current.workspace_id,
            for_update=True,
        )
        if stored_request is None:
            raise HandoffContextError("handoff target request was not found")
        target_execution = stored_execution
        target_request = stored_request
        source_execution = uow.executions.get(
            current.source_execution_id,
            current.workspace_id,
            for_update=True,
        )
        if source_execution is None:
            raise HandoffContextError("handoff source execution was not found")
        source_package = self._load_source_package(
            uow,
            current,
            source_facts,
            now=now,
        )
        source_manifest = self._load_source_manifest(
            uow,
            current,
            source_facts,
            source_execution=source_execution,
        )
        worker_item = uow.worker_items.get(
            current.worker_item_id,
            current.workspace_id,
            for_update=True,
        )
        if worker_item is None:
            raise HandoffContextError("handoff WorkerItem was not found")
        work_context = (
            None
            if current.handoff_schema_version != 2
            else uow.work_contexts.get_by_root_task(
                proposal.payload.root_task_id,
                current.workspace_id,
                for_update=True,
            )
        )
        context_revision = uow.work_contexts.get_revision(
            source_package.work_context_revision_id
            if work_context is None
            else work_context.current_revision_id,
            current.workspace_id,
        )
        if context_revision is None:
            raise HandoffContextError(
                "source WorkContext revision is no longer available",
                code="handoff_context_revision_missing",
            )
        self._validate_target_facts(
            current,
            proposal=proposal,
            target_request=target_request,
            target_execution=target_execution,
            worker_item=worker_item,
            context_revision=context_revision,
            source_package=source_package,
            source_manifest=source_manifest,
            source_facts=source_facts,
        )
        if current.handoff_schema_version == 2:
            binding = uow.agent_bindings.get(
                current.workspace_id,
                "worker_item",
                current.worker_item_id,
                for_update=True,
            )
            head = uow.plans.get_head(
                worker_item.root_task_id, current.workspace_id, for_update=True
            )
            if (
                work_context is None
                or binding is None
                or head is None
                or current.native_payload is None
                or (
                    work_context.id != current.native_payload.work_context_id
                    or binding.profile_ref != current.native_payload.target_profile_ref
                    or binding.version != current.target_binding_version
                    or not binding.runnable
                    or head.active_snapshot_id != target_request.plan_snapshot_id
                    or head.active_generation != target_request.plan_generation
                    or head.version != current.native_payload.observed_plan_head_version
                )
            ):
                raise HandoffContextError("native target context appointment changed")
        bundle_id = uuid5(
            _HANDOFF_CONTEXT_NAMESPACE,
            f"sidera:handoff-context-bundle:{current.id}:{current.logical_attempt}",
        )
        existing_bundle = uow.handoff_context_bundles.get_by_attempt(
            current.id,
            current.logical_attempt,
            current.workspace_id,
            for_update=True,
        )
        bundle_created_at = existing_bundle.created_at if existing_bundle else now
        candidate_bundle = _build_bundle(
            bundle_id=bundle_id,
            transition=current,
            proposal=proposal,
            target_execution=target_execution,
            source_facts=source_facts,
            source_manifest=source_manifest,
            created_at=bundle_created_at,
        )
        if existing_bundle is not None:
            if existing_bundle != candidate_bundle:
                raise HandoffContextError("handoff context Bundle facts changed")
            bundle = existing_bundle
        else:
            uow.handoff_context_bundles.add_append_only(candidate_bundle)
            bundle = candidate_bundle

        try:
            authorization = resolve_execution_context_authorization(
                uow, target_execution
            )
            request_id = uuid5(
                _HANDOFF_CONTEXT_NAMESPACE,
                f"sidera:handoff-context-request:{current.id}:"
                f"{current.logical_attempt}:{target_execution.id}",
            )
            idempotency_key_prefix = (
                f"context:handoff:{current.id}:attempt:{current.logical_attempt}:"
                f"execution:{target_execution.id}"
            )
            additional_budget = {
                "handoff_context_bundle_id": str(bundle.id),
                "handoff_context_bundle_hash": bundle.bundle_hash,
                "handoff_transition_id": str(current.id),
                "handoff_logical_attempt": current.logical_attempt,
            }
            if authorization is None:
                # Historical handoff fixtures and pre-selection plans have no
                # source request to inherit. Preserve their explicit v1
                # contract; a selection-bearing source never reaches here.
                request_candidate = build_execution_context_assembly_request(
                    execution=target_execution,
                    execution_request=target_request,
                    worker_item=worker_item,
                    work_context_revision=context_revision,
                    ttl_seconds=self._context_ttl_seconds,
                    created_at=bundle.created_at,
                    request_id=request_id,
                    idempotency_key=idempotency_key_prefix + ":v1",
                    additional_fact_scope_refs=("handoff_context_bundle",),
                    additional_budget=additional_budget,
                )
            else:
                request_candidate = build_derived_execution_context_assembly_request(
                    uow=uow,
                    execution=target_execution,
                    inheritance_kind="handoff",
                    source_execution_id=current.source_execution_id,
                    source_session_id=current.source_session_id,
                    ttl_seconds=self._context_ttl_seconds,
                    created_at=bundle.created_at,
                    request_id=request_id,
                    idempotency_key_prefix=idempotency_key_prefix,
                    additional_fact_scope_refs=("handoff_context_bundle",),
                    additional_budget=additional_budget,
                    work_context_revision=context_revision,
                )
        except (ExecutionContextAuthorizationError, ValueError, ValidationError) as exc:
            raise HandoffContextError(
                "handoff target input Selection authorization is invalid",
                code="handoff_context_input_selection_invalid",
            ) from exc
        if request_candidate is None:
            raise HandoffContextError(
                "handoff target input Selection authority is missing",
                code="handoff_context_input_selection_invalid",
            )
        request = uow.context_assemblies.add(request_candidate)
        if current.handoff_context_bundle_id is None:
            previous_version = current.version
            current.handoff_context_bundle_id = bundle.id
            current.version += 1
            uow.handoff_transitions.save(current, previous_version)
            _sync_transition(transition, current)
        elif current.handoff_context_bundle_id != bundle.id:
            raise HandoffContextError("handoff transition Bundle identity changed")
        return bundle, request

    def wait_until_package_ready(
        self,
        *,
        transition_id: UUID,
        workspace_id: UUID,
        now: datetime,
    ) -> ContextPackageManifestV1 | None:
        """Return a ready package, None while queued, or a stable terminal error."""

        _aware(now, "handoff context wait timestamp")
        with self._uow_factory() as uow:
            transition = uow.handoff_transitions.get(
                transition_id,
                workspace_id,
                for_update=True,
            )
            if transition is None:
                raise HandoffContextError("handoff transition was not found")
            target_execution_id = transition.target_execution_id
            if target_execution_id is None:
                raise HandoffContextError("handoff target execution is not created")
            if transition.handoff_context_bundle_id is None:
                raise HandoffContextError(
                    "handoff context Bundle is not created",
                    code="handoff_context_bundle_missing",
                )
            bundle = uow.handoff_context_bundles.get(
                transition.handoff_context_bundle_id,
                workspace_id,
                for_update=True,
            )
            if bundle is None or bundle.target_execution_id != target_execution_id:
                raise HandoffContextError(
                    "handoff context Bundle is stale",
                    code="handoff_context_bundle_stale",
                )
            target_execution = uow.executions.get(
                target_execution_id,
                workspace_id,
                for_update=True,
            )
            if target_execution is None:
                raise HandoffContextError(
                    "handoff target execution is missing",
                    code="handoff_target_execution_missing",
                )
            try:
                authorization = resolve_execution_context_authorization(
                    uow, target_execution
                )
                selection = (
                    load_execution_context_selection(uow, authorization.request)
                    if authorization is not None
                    else None
                )
                manifest_schema_version = execution_context_manifest_schema_version(
                    uow, target_execution
                )
            except (
                ExecutionContextAuthorizationError,
                ValueError,
                ValidationError,
            ) as exc:
                raise HandoffContextError(
                    "handoff target input Selection authorization is invalid",
                    code="handoff_context_input_selection_invalid",
                ) from exc
            assembly = uow.context_assemblies.get_for_consumer(
                ContextPurpose.EXECUTION,
                target_execution_id,
                workspace_id,
                manifest_schema_version=manifest_schema_version,
                for_update=True,
            )
            if assembly is None:
                return None
            if (
                str(assembly.budget.get("handoff_context_bundle_id"))
                != str(bundle.id)
                or assembly.budget.get("handoff_context_bundle_hash")
                != bundle.bundle_hash
            ):
                raise HandoffContextError(
                    "handoff context assembly does not reference the Bundle",
                    code="handoff_context_bundle_stale",
                )
            if assembly.status in _PENDING_ASSEMBLY_STATUSES:
                return None
            if assembly.status in {
                ContextAssemblyStatus.BLOCKED,
                ContextAssemblyStatus.FAILED,
            }:
                raise HandoffContextError(
                    "handoff target ContextPackage assembly is blocked",
                    code="handoff_context_package_blocked",
                    details={"error_code": assembly.error_code},
                )
            package = uow.context_packages.get_for_consumer(
                ContextPurpose.EXECUTION,
                target_execution_id,
                workspace_id,
                manifest_schema_version=manifest_schema_version,
                for_update=True,
            )
            if package is None:
                raise HandoffContextError(
                    "handoff target ContextPackage is missing",
                    code="handoff_context_package_missing",
                )
            if package.status is ContextPackageStatus.REVOKED:
                raise HandoffContextError(
                    "handoff target ContextPackage is revoked",
                    code="handoff_context_package_revoked",
                )
            if package.status is ContextPackageStatus.CORRUPT:
                raise HandoffContextError(
                    "handoff target ContextPackage is corrupt",
                    code="handoff_context_package_corrupt",
                )
            if package.status is not ContextPackageStatus.ACTIVE:
                raise HandoffContextError(
                    "handoff target ContextPackage is not active",
                    code="handoff_context_package_blocked",
                    details={"status": package.status.value},
                )
            if package.expires_at <= now:
                raise HandoffContextError(
                    "handoff target ContextPackage is expired",
                    code="handoff_context_package_expired",
                )
            if package.context_version != transition.target_context_version:
                raise HandoffContextError("handoff target ContextPackage version is stale")
            if (
                package.consumer_type is not ContextConsumerType.WORKER
                or package.consumer_id != target_execution_id
                or package.worker_item_id != transition.worker_item_id
                or package.work_context_revision_id != assembly.work_context_revision_id
                or package.idempotency_key != assembly.idempotency_key
            ):
                raise HandoffContextError(
                    "handoff target ContextPackage consumer facts are stale",
                    code="handoff_context_package_stale",
                )
            if selection is not None:
                result_input_manifest = getattr(package, "result_input_manifest", None)
                if (
                    assembly.manifest_schema_version not in {2, 3}
                    or assembly.manifest_schema_version != manifest_schema_version
                    or assembly.input_selection_id != selection.id
                    or assembly.input_selection_hash != selection.selection_hash
                    or package.manifest_schema_version != manifest_schema_version
                    or not isinstance(result_input_manifest, ResultInputManifestV1)
                    or result_input_manifest.input_selection_id != selection.id
                    or result_input_manifest.input_selection_hash
                    != selection.selection_hash
                ):
                    raise HandoffContextError(
                        "handoff target input Selection package is stale",
                        code="handoff_context_input_selection_stale",
                    )
            if manifest_schema_version == 3:
                if authorization is None or selection is None:
                    raise HandoffContextError(
                        "handoff target code input Selection is missing",
                        code="handoff_context_input_selection_stale",
                    )
                self._validate_code_package(
                    uow,
                    execution=target_execution,
                    request=authorization.request,
                    selection=selection,
                    assembly=assembly,
                    package=package,
                    error_prefix="handoff_context_input_selection",
                )
            handoff_facts = package.control_plane_fact_snapshot.get("handoff")
            if (
                not isinstance(handoff_facts, Mapping)
                or str(handoff_facts.get("bundle_id")) != str(bundle.id)
                or handoff_facts.get("bundle_hash") != bundle.bundle_hash
                or str(handoff_facts.get("transition_id")) != str(transition.id)
                or handoff_facts.get("logical_attempt") != transition.logical_attempt
            ):
                raise HandoffContextError(
                    "handoff target ContextPackage does not contain the Bundle facts",
                    code="handoff_context_package_stale",
                )
            if transition.target_context_package_id is not None and (
                transition.target_context_package_id != package.package_id
                or transition.target_context_package_hash != package.package_hash
            ):
                raise HandoffContextError("handoff target ContextPackage identity changed")
            if transition.target_context_package_id is None:
                previous_version = transition.version
                transition.target_context_package_id = package.package_id
                transition.target_context_package_hash = package.package_hash
                transition.version += 1
                uow.handoff_transitions.save(transition, previous_version)
            return package

    @staticmethod
    def _load_source_package(
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        source_facts: HandoffSourceFacts,
        *,
        now: datetime,
    ) -> ContextPackageManifestV1:
        package = uow.context_packages.get(
            source_facts.checkpoint.context_package_id,
            transition.workspace_id,
            for_update=True,
        )
        if package is None:
            raise HandoffContextError("handoff source ContextPackage was not found")
        if package.package_hash != source_facts.checkpoint.context_package_hash:
            raise HandoffContextError("handoff source ContextPackage facts changed")
        if package.status is ContextPackageStatus.REVOKED:
            raise HandoffContextError(
                "handoff source ContextPackage is revoked",
                code="handoff_context_source_package_revoked",
            )
        if package.status is ContextPackageStatus.CORRUPT:
            raise HandoffContextError(
                "handoff source ContextPackage is corrupt",
                code="handoff_context_source_package_corrupt",
            )
        if package.expires_at <= now:
            raise HandoffContextError(
                "handoff source ContextPackage is expired",
                code="handoff_context_source_package_expired",
            )
        if (
            package.status is not ContextPackageStatus.ACTIVE
            or package.purpose is not ContextPurpose.EXECUTION
            or package.consumer_type is not ContextConsumerType.WORKER
            or package.consumer_id != transition.source_execution_id
            or package.worker_item_id != transition.worker_item_id
            or package.context_version != source_facts.source_worker_context_version
        ):
            raise HandoffContextError("handoff source ContextPackage facts changed")
        source_execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
        )
        try:
            authorization = (
                resolve_execution_context_authorization(uow, source_execution)
                if source_execution is not None
                else None
            )
            selection = (
                load_execution_context_selection(uow, authorization.request)
                if authorization is not None
                else None
            )
            manifest_schema_version = (
                execution_context_manifest_schema_version(uow, source_execution)
                if source_execution is not None
                else 1
            )
        except (ExecutionContextAuthorizationError, ValueError, ValidationError) as exc:
            raise HandoffContextError(
                "handoff source input Selection authorization is invalid",
                code="handoff_context_source_input_selection_invalid",
            ) from exc
        if package.manifest_schema_version != manifest_schema_version:
            raise HandoffContextError(
                "handoff source ContextPackage input protocol is stale",
                code="handoff_context_source_input_selection_stale",
            )
        if selection is not None:
            result_input_manifest = getattr(package, "result_input_manifest", None)
            if (
                manifest_schema_version not in {2, 3}
                or not isinstance(result_input_manifest, ResultInputManifestV1)
                or result_input_manifest.input_selection_id != selection.id
                or result_input_manifest.input_selection_hash
                != selection.selection_hash
            ):
                raise HandoffContextError(
                    "handoff source ContextPackage input Selection is stale",
                    code="handoff_context_source_input_selection_stale",
                )
        if manifest_schema_version == 3:
            if authorization is None or selection is None or source_execution is None:
                raise HandoffContextError(
                    "handoff source code input Selection is missing",
                    code="handoff_context_source_input_selection_stale",
                )
            assembly = uow.context_assemblies.get_for_consumer(
                ContextPurpose.EXECUTION,
                source_execution.id,
                transition.workspace_id,
                manifest_schema_version=manifest_schema_version,
            )
            HandoffContextService._validate_code_package(
                uow,
                execution=source_execution,
                request=authorization.request,
                selection=selection,
                assembly=assembly,
                package=package,
                error_prefix="handoff_context_source_input_selection",
            )
        return package

    @staticmethod
    def _validate_code_package(
        uow: ControlPlaneUnitOfWork,
        *,
        execution: Execution,
        request: ExecutionRequest,
        selection: CollaborationInputSelectionV1,
        assembly: ContextAssemblyRequest | None,
        package: ContextPackageManifestV1,
        error_prefix: str,
    ) -> None:
        manifest = getattr(package, "code_input_manifest", None)
        if (
            not isinstance(package, ContextPackageManifestV3)
            or not isinstance(manifest, CodeInputManifestV1)
            or assembly is None
            or assembly.manifest_schema_version != 3
            or not isinstance(assembly.code_input_manifest, CodeInputManifestV1)
            or assembly.code_input_manifest != manifest
            or assembly.verification_manifest is not None
            or package.verification_manifest is not None
            or manifest.workspace_id != execution.workspace_id
            or manifest.workspace_id != request.workspace_id
            or manifest.root_task_id != request.root_task_id
            or manifest.request_id != request.id
            or manifest.execution_id != execution.id
            or manifest.selection_id != selection.id
            or manifest.selection_hash != selection.selection_hash
            or manifest.input_hash != manifest.compute_content_hash()
            or assembly.input_selection_id != selection.id
            or assembly.input_selection_hash != selection.selection_hash
            or assembly.budget.get("code_input_hash") != manifest.input_hash
            or assembly.budget.get("execution_request_id") != str(request.id)
            or assembly.work_context_revision_id != package.work_context_revision_id
            or assembly.context_version != package.context_version
            or assembly.idempotency_key != package.idempotency_key
            or ":v3:" not in assembly.idempotency_key
            or manifest.input_hash not in assembly.idempotency_key
        ):
            raise HandoffContextError(
                "handoff code input package identity is stale",
                code=f"{error_prefix}_stale",
            )
        try:
            resolve_code_inputs(uow, manifest)
        except (ResultError, ValueError, ValidationError) as exc:
            raise HandoffContextError(
                "handoff code input Selection authorization is invalid",
                code=f"{error_prefix}_invalid",
            ) from exc

    @staticmethod
    def _load_source_manifest(
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        source_facts: HandoffSourceFacts,
        *,
        source_execution: Execution,
    ) -> ExecutionWorkspaceManifest:
        manifest = uow.workspaces.get(
            transition.source_workspace_manifest_id,
            transition.workspace_id,
            for_update=True,
        )
        if manifest is None:
            raise HandoffContextError("handoff source Workspace was not found")
        if (
            manifest.lifecycle is not WorkspaceLifecycle.FROZEN
            or manifest.execution_id != transition.source_execution_id
            or manifest.task_id != source_execution.task_id
            or workspace_manifest_digest(manifest)
            != (
                source_facts.workspace_state.get("workspace_digest")
                if transition.handoff_schema_version == 2
                else source_facts.checkpoint.workspace_digest
            )
        ):
            raise HandoffContextError("handoff source Workspace facts changed")
        state = source_facts.workspace_state
        for key, expected in (
            ("manifest_id", str(manifest.id)),
            ("workspace_digest", workspace_manifest_digest(manifest)),
            ("fencing_token", manifest.fencing_token),
        ):
            if key in state and state[key] != expected:
                raise HandoffContextError("handoff source Workspace facts changed")
        return manifest

    @staticmethod
    def _validate_target_facts(
        transition: HandoffTransition,
        *,
        proposal: HandoffProposal,
        target_request: ExecutionRequest,
        target_execution: Execution,
        worker_item: WorkerItem,
        context_revision: WorkContextRevision,
        source_package: ContextPackageManifestV1,
        source_manifest: ExecutionWorkspaceManifest,
        source_facts: HandoffSourceFacts,
    ) -> None:
        payload = proposal.payload
        if (
            target_request.workspace_id != transition.workspace_id
            or target_request.worker_item_id != transition.worker_item_id
            or target_request.status is not ExecutionRequestStatus.MATERIALIZED
            or target_request.purpose != "handoff"
            or target_request.handoff_transition_id != transition.id
            or target_request.source_execution_id != transition.source_execution_id
            or target_request.source_session_id != transition.source_session_id
            or target_request.root_task_id != payload.root_task_id
            or target_request.subtask_id != payload.subtask_id
            or target_request.execution_lineage_id != target_execution.lineage_id
            or target_request.first_execution_id != target_execution.id
            or target_request.plan_snapshot_id != payload.observed_plan_snapshot_id
            or target_request.plan_generation != payload.observed_plan_generation
        ):
            raise HandoffContextError("handoff target request facts changed")
        if (
            target_execution.workspace_id != transition.workspace_id
            or target_execution.worker_item_id != transition.worker_item_id
            or target_execution.status is not ExecutionStatus.QUEUED
            or target_execution.pinned_worker_id != transition.target_worker_id
            or target_execution.handoff_transition_id != transition.id
            or target_execution.source_execution_id != transition.source_execution_id
            or target_execution.source_session_id != transition.source_session_id
            or target_execution.task_id != payload.subtask_id
            or target_execution.last_fencing_token
            != source_manifest.fencing_token
        ):
            raise HandoffContextError("handoff target execution facts changed")
        if worker_item.id != transition.worker_item_id:
            raise HandoffContextError("handoff WorkerItem identity changed")
        if worker_item.context_version != transition.target_context_version:
            raise HandoffContextError("handoff target context version changed")
        if context_revision.workspace_id != transition.workspace_id:
            raise HandoffContextError("handoff WorkContext workspace changed")
        if context_revision.plan_snapshot_id not in {
            None,
            target_request.plan_snapshot_id,
        } or context_revision.proposal_revision_id not in {
            None,
            target_request.proposal_revision_id,
        }:
            raise HandoffContextError("handoff WorkContext revision is stale")
        if (
            transition.handoff_schema_version != 2
            and source_package.work_context_revision_id != context_revision.revision_id
        ):
            raise HandoffContextError("handoff source WorkContext revision changed")
        if source_facts.input_classification is None:
            raise HandoffContextError("handoff input classification is missing")
        if (
            source_facts.source_context_package_ref
            != context_package_reference(
                source_facts.checkpoint.context_package_id,
                source_facts.checkpoint.context_package_hash,
            )
            or source_facts.source_worker_context_version
            != source_facts.checkpoint.context_version
            or source_facts.artifact_refs != source_facts.checkpoint.artifact_refs
            or source_facts.evidence_refs != source_facts.checkpoint.evidence_refs
        ):
            raise HandoffContextError("handoff source facts changed")


def _build_bundle(
    *,
    bundle_id: UUID,
    transition: HandoffTransition,
    proposal: HandoffProposal,
    target_execution: Execution,
    source_facts: HandoffSourceFacts,
    source_manifest: ExecutionWorkspaceManifest,
    created_at: datetime,
) -> HandoffContextBundle:
    classification = source_facts.input_classification
    if classification is None:
        raise HandoffContextError("handoff input classification is missing")
    if transition.target_context_version is None:
        raise HandoffContextError("handoff target context version is missing")
    delivered = tuple(
        item.source_input_id
        for item in classification.dispositions
        if item.action is HandoffInputAction.PRESERVE_DELIVERED
    )
    repository_state = tuple(
        {
            "repository_ref": checkout.repository_ref,
            "base_revision": checkout.base_revision,
            "branch": checkout.branch,
            "current_head": checkout.current_head,
        }
        for checkout in source_manifest.checkouts
    )
    return HandoffContextBundle.create(
        id=bundle_id,
        schema_version=transition.handoff_schema_version,
        workspace_id=transition.workspace_id,
        worker_item_id=transition.worker_item_id,
        handoff_transition_id=transition.id,
        logical_attempt=transition.logical_attempt,
        recovery_decision_id=None,
        source_execution_id=transition.source_execution_id,
        source_session_id=transition.source_session_id,
        source_checkpoint_sequence=source_facts.checkpoint.accepted_sequence,
        source_last_accepted_sequence=source_facts.stop.accepted_sequence,
        source_context_package_id=source_facts.checkpoint.context_package_id,
        source_context_package_hash=source_facts.checkpoint.context_package_hash,
        source_worker_context_version=source_facts.source_worker_context_version,
        target_execution_id=target_execution.id,
        target_worker_id=transition.target_worker_id,
        target_runtime_snapshot_hash=None
        if transition.handoff_schema_version == 2
        else _hash_json(transition.target_runtime_snapshot),
        target_profile_ref=None
        if transition.native_payload is None
        else transition.native_payload.target_profile_ref,
        target_binding_version=transition.target_binding_version,
        target_native_metadata=None
        if transition.handoff_schema_version != 2
        else {
            "execution_mode": "native_profile",
            "capability_requirements": sorted(target_execution.capability_requirements),
        },
        target_context_version=transition.target_context_version,
        source_workspace_manifest_id=source_manifest.id,
        source_workspace_version=source_manifest.version,
        workspace_location_ref_hash=workspace_location_ref_hash(
            source_manifest.location_ref
        ),
        repository_state=repository_state,
        artifact_refs=source_facts.artifact_refs,
        evidence_refs=source_facts.evidence_refs,
        handoff_reason=proposal.payload.handoff_reason,
        handoff_instruction=proposal.payload.handoff_instruction,
        delivered_input_refs=delivered,
        transferable_input_refs=classification.transferable_input_ids,
        excluded_input_refs=classification.excluded_input_ids,
        created_at=created_at,
    )


def _validate_input_classification(
    transition: HandoffTransition,
    classification: HandoffInputClassification,
) -> None:
    if not isinstance(classification, HandoffInputClassification):
        raise HandoffContextError("handoff input classification is invalid")
    if (
        classification.source_session_id != transition.source_session_id
        or classification.cutoff_sequence != transition.source_input_cutoff_sequence
    ):
        raise HandoffContextError("handoff input classification base changed")
    if classification.unknown_input_ids or any(
        item.action is HandoffInputAction.BLOCK
        or item.source_status.strip().lower() in {"unknown", "in_flight", "in-flight"}
        for item in classification.dispositions
    ):
        raise HandoffContextError(
            "handoff input classification contains unknown inputs",
            code="handoff_context_input_unknown",
        )


def _checkpoint_from_transition(
    transition: HandoffTransition,
) -> HandoffCheckpointEvidence:
    facts = transition.source_checkpoint_facts
    if transition.source_checkpoint_sequence is None or facts is None:
        raise HandoffContextError(
            "handoff source checkpoint is incomplete",
            code="handoff_context_source_checkpoint_missing",
        )
    try:
        if transition.handoff_schema_version == 2:
            evidence = HandoffCheckpointEvidence.from_native_mapping(facts)
            if evidence.accepted_sequence != transition.source_checkpoint_sequence:
                raise HandoffContextError("handoff checkpoint sequence changed")
            return evidence
        evidence = HandoffCheckpointEvidence(
            operation_id=UUID(str(facts["operation_id"])),
            transition_id=UUID(str(facts["transition_id"])),
            source_session_id=UUID(str(facts["source_session_id"])),
            accepted_sequence=int(str(facts["accepted_sequence"])),
            workspace_digest=str(facts["workspace_digest"]),
            context_package_id=UUID(str(facts["context_package_id"])),
            context_package_hash=str(facts["context_package_hash"]),
            context_version=int(str(facts["context_version"])),
            progress_summary_ref=str(facts["progress_summary_ref"]),
            artifact_refs=_fact_refs(facts.get("artifact_refs")),
            evidence_refs=_fact_refs(facts.get("evidence_refs")),
            quiescent=_fact_bool(facts["quiescent"], "quiescent"),
            produced_at=datetime.fromisoformat(str(facts["produced_at"])),
            schema_version=int(str(facts.get("schema_version", 1))),
        )
    except (DomainError, KeyError, TypeError, ValueError) as exc:
        raise HandoffContextError(
            "persisted handoff checkpoint facts are invalid",
            code="handoff_context_source_checkpoint_invalid",
        ) from exc
    if evidence.accepted_sequence != transition.source_checkpoint_sequence:
        raise HandoffContextError("handoff checkpoint sequence changed")
    return evidence


def _stop_from_transition(transition: HandoffTransition) -> HandoffStopEvidence:
    facts = transition.source_stop_facts
    if transition.source_stop_sequence is None or facts is None:
        raise HandoffContextError(
            "handoff source stop is incomplete",
            code="handoff_context_source_stop_missing",
        )
    try:
        if transition.handoff_schema_version == 2:
            evidence = HandoffStopEvidence.from_native_mapping(facts)
            if evidence.accepted_sequence != transition.source_stop_sequence:
                raise HandoffContextError("handoff stop sequence changed")
            return evidence
        evidence = HandoffStopEvidence(
            operation_id=UUID(str(facts["operation_id"])),
            source_session_id=UUID(str(facts["source_session_id"])),
            accepted_sequence=int(str(facts["accepted_sequence"])),
            stopped=_fact_bool(facts["stopped"], "stopped"),
            produced_at=datetime.fromisoformat(str(facts["produced_at"])),
        )
    except (DomainError, KeyError, TypeError, ValueError) as exc:
        raise HandoffContextError(
            "persisted handoff stop facts are invalid",
            code="handoff_context_source_stop_invalid",
        ) from exc
    if evidence.accepted_sequence != transition.source_stop_sequence:
        raise HandoffContextError("handoff stop sequence changed")
    return evidence


def _fact_refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("handoff evidence refs must be a sequence")
    return tuple(str(item) for item in value)


def _fact_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"handoff {name} must be a boolean")
    return value


def _safe_workspace_state(facts: Mapping[str, object]) -> dict[str, object]:
    return {
        key: facts[key]
        for key in _SAFE_WORKSPACE_STATE_KEYS
        if key in facts and key != "location_ref"
    }


def _sync_transition(target: HandoffTransition, source: HandoffTransition) -> None:
    target.handoff_context_bundle_id = source.handoff_context_bundle_id
    target.target_context_package_id = source.target_context_package_id
    target.target_context_package_hash = source.target_context_package_hash
    target.version = source.version


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
