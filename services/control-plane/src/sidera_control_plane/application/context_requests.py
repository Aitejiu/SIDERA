from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.collaboration import RuntimeRelease
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextConsumerType,
    ContextPurpose,
    ResultInputBudgetV1,
    VerificationManifestV1,
)
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionSession,
    ExecutionSessionStatus,
)
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.input_selections import CollaborationInputSelectionV1
from sidera_control_plane.domain.plan_revisions import RevisionBaseDescriptorV1
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
    PlanningRequestKind,
    PlanningRun,
)
from sidera_control_plane.domain.work_context import WorkContextRevision
from sidera_control_plane.domain.worker_item import WorkerItem

DEFAULT_CONTEXT_MAX_SECTIONS = 16
DEFAULT_CONTEXT_MAX_CHARS = 64 * 1024
DEFAULT_CONTEXT_MAX_INPUT_TOKENS = 16 * 1024
EXECUTION_CONTEXT_PROFILE_NAMESPACE = NAMESPACE_URL


class ExecutionContextAuthorizationError(ValueError):
    """Raised when a derived execution cannot prove its context authority."""


@dataclass(frozen=True, slots=True)
class ExecutionContextAuthorization:
    request: ExecutionRequest
    source_execution_id: UUID | None
    source_session_id: UUID | None
    source_lineage_id: UUID
    inherited: bool


def resolve_execution_context_authorization(
    uow: ControlPlaneUnitOfWork,
    execution: Execution,
) -> ExecutionContextAuthorization | None:
    """Resolve context authority through a request or explicit execution links.

    A WorkerItem is deliberately not used as a fallback identity. If the
    current execution has no request, every parent hop must be represented by
    its persisted source execution and source session fields.
    """

    current = execution
    first_source_execution_id: UUID | None = None
    first_source_session_id: UUID | None = None
    visited: set[UUID] = set()
    for _ in range(64):
        if current.id in visited:
            raise ExecutionContextAuthorizationError(
                "execution context inheritance contains a cycle"
            )
        visited.add(current.id)
        request = uow.execution_requests.get_effective_by_execution_lineage(
            current.lineage_id,
            execution.workspace_id,
        )
        if request is not None:
            if request.status is not ExecutionRequestStatus.MATERIALIZED:
                raise ExecutionContextAuthorizationError(
                    "execution context request is not materialized"
                )
            if request.workspace_id != execution.workspace_id:
                raise ExecutionContextAuthorizationError(
                    "execution context request workspace does not match execution"
                )
            if request.worker_item_id != execution.worker_item_id:
                raise ExecutionContextAuthorizationError(
                    "execution context request WorkerItem does not match execution"
                )
            _validate_direct_request_source(
                uow,
                execution=current,
                request=request,
            )
            # Handoff targets intentionally keep an unmanaged request. Their
            # fixed result-input authority lives on the source lineage, so a
            # target request without a Selection must continue through its
            # explicit source execution/session link.
            if (
                request.input_selection_id is None
                and request.source_execution_id is not None
                and request.source_session_id is not None
            ):
                if first_source_execution_id is None:
                    first_source_execution_id = request.source_execution_id
                    first_source_session_id = request.source_session_id
                parent = uow.executions.get(
                    request.source_execution_id,
                    execution.workspace_id,
                )
                source_session = uow.sessions.get_any(
                    request.source_session_id,
                    execution.workspace_id,
                )
                _validate_source_link(
                    child=current,
                    parent=parent,
                    source_session=source_session,
                )
                assert parent is not None
                current = parent
                continue
            request_lineage_id = request.execution_lineage_id or current.lineage_id
            return ExecutionContextAuthorization(
                request=request,
                source_execution_id=first_source_execution_id,
                source_session_id=first_source_session_id,
                source_lineage_id=request_lineage_id,
                inherited=first_source_execution_id is not None,
            )

        source_execution_id = current.source_execution_id
        source_session_id = current.source_session_id
        if source_execution_id is None and source_session_id is None:
            return None
        if source_execution_id is None or source_session_id is None:
            raise ExecutionContextAuthorizationError(
                "execution context inheritance source link is incomplete"
            )
        parent = uow.executions.get(source_execution_id, execution.workspace_id)
        source_session = uow.sessions.get_any(
            source_session_id,
            execution.workspace_id,
        )
        if first_source_execution_id is None:
            first_source_execution_id = source_execution_id
            first_source_session_id = source_session_id
        _validate_source_link(
            child=current,
            parent=parent,
            source_session=source_session,
        )
        assert parent is not None
        current = parent
    raise ExecutionContextAuthorizationError(
        "execution context inheritance exceeds the maximum depth"
    )


def load_execution_context_selection(
    uow: ControlPlaneUnitOfWork,
    request: ExecutionRequest,
) -> CollaborationInputSelectionV1 | None:
    """Load and validate the immutable Selection bound to an execution request."""

    if request.input_selection_id is None:
        return None
    selection = uow.input_selections.get(
        request.workspace_id,
        request.input_selection_id,
    )
    release = uow.collaboration_ledger.get_release_for_request(
        request.workspace_id,
        request.id,
    )
    if selection is None or release is None:
        raise ValueError("selection-bound execution is missing its release or Selection")
    validate_execution_context_selection(
        execution_request=request,
        release=release,
        selection=selection,
    )
    return selection


def build_derived_execution_context_assembly_request(
    *,
    uow: ControlPlaneUnitOfWork,
    execution: Execution,
    inheritance_kind: str,
    source_execution_id: UUID,
    source_session_id: UUID | None,
    ttl_seconds: int,
    created_at: datetime,
    request_id: UUID | None = None,
    idempotency_key: str | None = None,
    additional_fact_scope_refs: Sequence[str] = (),
    additional_budget: Mapping[str, object] | None = None,
    work_context_revision: WorkContextRevision | None = None,
) -> ContextAssemblyRequest | None:
    """Build a new consumer package request for a derived execution."""

    authorization = resolve_execution_context_authorization(uow, execution)
    if authorization is None:
        return None
    if not inheritance_kind.strip():
        raise ValueError("execution context inheritance kind is required")
    clean_kind = inheritance_kind.strip()
    if clean_kind == "continuation":
        if authorization.source_execution_id != source_execution_id:
            raise ExecutionContextAuthorizationError(
                "execution context inheritance source execution does not match"
            )
        if authorization.source_session_id != source_session_id:
            raise ExecutionContextAuthorizationError(
                "execution context inheritance source Session does not match"
            )
    elif clean_kind == "handoff":
        if not authorization.inherited or (
            authorization.source_execution_id != execution.source_execution_id
            or authorization.source_session_id != execution.source_session_id
            or authorization.source_execution_id != source_execution_id
            or authorization.source_session_id != source_session_id
        ):
            raise ExecutionContextAuthorizationError(
                "execution context handoff source link does not match"
            )
    elif clean_kind == "retry":
        if authorization.inherited and (
            authorization.source_execution_id != source_execution_id
            or authorization.source_session_id != source_session_id
        ):
            raise ExecutionContextAuthorizationError(
                "execution context retry source link does not match"
            )
    else:
        raise ExecutionContextAuthorizationError(
            "unsupported execution context inheritance kind"
        )
    if execution.worker_item_id is None:
        raise ExecutionContextAuthorizationError(
            "derived execution has no WorkerItem for context inheritance"
        )
    worker_item = uow.worker_items.get(execution.worker_item_id, execution.workspace_id)
    if worker_item is None:
        raise ExecutionContextAuthorizationError(
            "derived execution WorkerItem is unavailable"
        )
    revision = work_context_revision
    if revision is None:
        work_context = uow.work_contexts.get_by_planning_request(
            authorization.request.planning_request_id,
            execution.workspace_id,
        )
        if work_context is None:
            work_context = uow.work_contexts.get_by_root_task(
                authorization.request.root_task_id,
                execution.workspace_id,
            )
        if work_context is not None:
            revision = uow.work_contexts.get_revision(
                work_context.current_revision_id,
                execution.workspace_id,
            )
    if revision is None:
        raise ExecutionContextAuthorizationError(
            "derived execution WorkContext revision is unavailable"
        )
    selection = load_execution_context_selection(uow, authorization.request)
    inherited_budget: dict[str, object] = {
        "execution_context_inheritance": {
            "kind": clean_kind,
            "authorization_request_id": str(authorization.request.id),
            "source_execution_id": str(source_execution_id),
            "source_session_id": (
                str(source_session_id) if source_session_id is not None else None
            ),
            "source_lineage_id": str(authorization.source_lineage_id),
        }
    }
    if additional_budget:
        overlap = set(inherited_budget).intersection(additional_budget)
        if overlap:
            raise ValueError(
                "derived execution context budget already owns: "
                + ", ".join(sorted(overlap))
            )
        inherited_budget.update(additional_budget)
    return build_execution_context_assembly_request(
        execution=execution,
        execution_request=authorization.request,
        worker_item=worker_item,
        work_context_revision=revision,
        ttl_seconds=ttl_seconds,
        created_at=created_at,
        request_id=request_id,
        idempotency_key=idempotency_key,
        additional_fact_scope_refs=(
            "execution_context_inheritance",
            *additional_fact_scope_refs,
        ),
        additional_budget=inherited_budget,
        manifest_schema_version=2 if selection is not None else 1,
        input_selection=selection,
    )


def _validate_direct_request_source(
    uow: ControlPlaneUnitOfWork,
    *,
    execution: Execution,
    request: ExecutionRequest,
) -> None:
    request_source_execution_id = request.source_execution_id
    request_source_session_id = request.source_session_id
    execution_source_execution_id = execution.source_execution_id
    execution_source_session_id = execution.source_session_id
    if (request_source_execution_id, request_source_session_id) != (
        execution_source_execution_id,
        execution_source_session_id,
    ):
        if any(
            value is not None
            for value in (
                request_source_execution_id,
                request_source_session_id,
                execution_source_execution_id,
                execution_source_session_id,
            )
        ):
            raise ExecutionContextAuthorizationError(
                "execution context request source link does not match execution"
            )
        return
    if request_source_execution_id is None:
        return
    if request_source_session_id is None:
        raise ExecutionContextAuthorizationError(
            "execution context request source Session is incomplete"
        )
    parent = uow.executions.get(request_source_execution_id, execution.workspace_id)
    source_session = uow.sessions.get_any(
        request_source_session_id,
        execution.workspace_id,
    )
    _validate_source_link(
        child=execution,
        parent=parent,
        source_session=source_session,
    )


def _validate_source_link(
    *,
    child: Execution,
    parent: Execution | None,
    source_session: ExecutionSession | None,
) -> None:
    if parent is None or source_session is None:
        raise ExecutionContextAuthorizationError(
            "execution context inheritance source lineage is unavailable"
        )
    if parent.workspace_id != child.workspace_id:
        raise ExecutionContextAuthorizationError(
            "execution context inheritance crosses workspace"
        )
    if parent.worker_item_id != child.worker_item_id:
        raise ExecutionContextAuthorizationError(
            "execution context inheritance changes WorkerItem"
        )
    if source_session.workspace_id != child.workspace_id:
        raise ExecutionContextAuthorizationError(
            "execution context inheritance Session crosses workspace"
        )
    if source_session.execution_id != parent.id:
        raise ExecutionContextAuthorizationError(
            "execution context inheritance Session does not belong to source execution"
        )
    if parent.status not in TERMINAL_EXECUTION_STATUSES:
        raise ExecutionContextAuthorizationError(
            "execution context source execution is not terminal"
        )
    if source_session.status is ExecutionSessionStatus.ACTIVE:
        raise ExecutionContextAuthorizationError(
            "execution context source Session is still active"
        )


def build_planning_context_assembly_request(
    *,
    run: PlanningRun,
    brief: PlanningBriefRevision,
    work_context_revision: WorkContextRevision,
    ttl_seconds: int,
    created_at: datetime,
    planning_kind: PlanningRequestKind = PlanningRequestKind.CREATE,
    revision_base: RevisionBaseDescriptorV1 | None = None,
) -> ContextAssemblyRequest:
    if planning_kind is PlanningRequestKind.REVISION and revision_base is None:
        raise ValueError("revision planning assembly requires an exact base")
    if planning_kind is PlanningRequestKind.CREATE and revision_base is not None:
        raise ValueError("create planning assembly cannot carry a revision base")
    profile = run.profile_snapshot
    budget = {
        "profile_id": str(profile.profile_id),
        "profile_version": profile.profile_version,
        "max_sections": DEFAULT_CONTEXT_MAX_SECTIONS,
        "max_chars": profile.budget.max_context_bytes,
        "max_input_tokens": profile.budget.max_tokens,
        "planner_module_instance_id": str(profile.planner_module_instance_id),
        "planner_module_config_version": profile.planner_module_config_version,
        "planner_interface_version": profile.planner_interface_version,
        "prompt_policy_version": profile.prompt_policy_version,
        "proposal_schema_version": profile.proposal_schema_version,
        "planning_kind": planning_kind.value,
    }
    if revision_base is not None:
        budget["revision_base"] = {
            "schema_version": revision_base.schema_version,
            "workspace_id": str(revision_base.workspace_id),
            "root_task_id": str(revision_base.root_task_id),
            "plan_id": str(revision_base.plan_snapshot_id),
            "generation": revision_base.plan_generation,
            "content_hash": revision_base.plan_content_hash,
            "root_task_version": revision_base.root_task_version,
            "captured_at": revision_base.captured_at.isoformat(),
        }
    search_scope = _search_scope(brief.objective, brief.requested_outputs)
    filter_refs = tuple(
        item.to_mapping() for item in work_context_revision.snapshot.knowledge_refs
    )
    fact_scope_refs = (
        "planning_request",
        "planning_run",
        "brief",
        "planning_profile",
        "planner_module",
    )
    if planning_kind is PlanningRequestKind.REVISION:
        fact_scope_refs += ("revision_base",)
    expires_at = _expires_at(created_at, ttl_seconds)
    fingerprint = _request_fingerprint(
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=run.id,
        worker_item_id=None,
        work_context_revision_id=work_context_revision.revision_id,
        context_version=None,
        search_scope=search_scope,
        filter_refs=filter_refs,
        fact_scope_refs=fact_scope_refs,
        budget=budget,
        expires_at=expires_at,
    )
    return ContextAssemblyRequest.create(
        request_id=uuid4(),
        workspace_id=run.workspace_id,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=run.id,
        worker_item_id=None,
        work_context_revision_id=work_context_revision.revision_id,
        context_version=None,
        knowledge_search_scope=search_scope,
        knowledge_filter_refs=filter_refs,
        fact_scope_refs=fact_scope_refs,
        budget=budget,
        expires_at=expires_at,
        request_fingerprint=fingerprint,
        idempotency_key=f"context:planning-run:{run.id}:v1",
        created_at=created_at,
    )


def build_execution_context_assembly_request(
    *,
    execution: Execution,
    execution_request: ExecutionRequest,
    worker_item: WorkerItem,
    work_context_revision: WorkContextRevision,
    ttl_seconds: int,
    created_at: datetime,
    request_id: UUID | None = None,
    idempotency_key: str | None = None,
    additional_fact_scope_refs: Sequence[str] = (),
    additional_budget: Mapping[str, object] | None = None,
    manifest_schema_version: int = 1,
    input_selection: CollaborationInputSelectionV1 | None = None,
    result_input_budget: ResultInputBudgetV1 | None = None,
    code_input_manifest: CodeInputManifestV1 | None = None,
    verification_manifest: VerificationManifestV1 | None = None,
) -> ContextAssemblyRequest:
    if execution.worker_item_id != worker_item.id:
        raise ValueError("execution and WorkerItem do not match")
    if manifest_schema_version == 1:
        if any(
            value is not None
            for value in (
                input_selection,
                result_input_budget,
                code_input_manifest,
                verification_manifest,
            )
        ):
            raise ValueError("v1 execution context cannot carry execution input")
    elif manifest_schema_version == 2:
        if not isinstance(input_selection, CollaborationInputSelectionV1):
            raise ValueError("v2 execution context requires an input selection")
        _validate_selection_target(
            execution_request=execution_request,
            worker_item=worker_item,
            selection=input_selection,
        )
        result_input_budget = result_input_budget or ResultInputBudgetV1()
        if code_input_manifest is not None or verification_manifest is not None:
            raise ValueError("v2 execution context cannot carry execution input manifests")
    elif manifest_schema_version == 3:
        if code_input_manifest is None and verification_manifest is None:
            raise ValueError("v3 execution context requires an execution input manifest")
        if code_input_manifest is not None and verification_manifest is not None:
            raise ValueError("v3 execution context cannot mix code and verification inputs")
        if code_input_manifest is not None:
            if not isinstance(code_input_manifest, CodeInputManifestV1):
                raise ValueError("v3 code input manifest must be typed")
            if not isinstance(input_selection, CollaborationInputSelectionV1):
                raise ValueError("v3 code execution context requires an input selection")
            _validate_selection_target(
                execution_request=execution_request,
                worker_item=worker_item,
                selection=input_selection,
            )
            if (
                code_input_manifest.workspace_id != execution.workspace_id
                or code_input_manifest.root_task_id != execution_request.root_task_id
                or code_input_manifest.request_id != execution_request.id
                or code_input_manifest.execution_id != execution.id
                or code_input_manifest.selection_id != input_selection.id
                or code_input_manifest.selection_hash != input_selection.selection_hash
            ):
                raise ValueError("v3 code input manifest scope does not match execution")
            result_input_budget = result_input_budget or ResultInputBudgetV1()
        elif input_selection is not None or result_input_budget is not None:
            raise ValueError("v3 verification context cannot carry code selection fields")
        if verification_manifest is not None and not isinstance(
            verification_manifest, VerificationManifestV1
        ):
            raise ValueError("v3 verification manifest must be typed")
    else:
        raise ValueError("unsupported execution context manifest schema version")
    profile_id = uuid5(
        EXECUTION_CONTEXT_PROFILE_NAMESPACE,
        f"sidera:execution-context-profile:{execution_request.budget_class}:v1",
    )
    budget = {
        "profile_id": str(profile_id),
        "profile_version": 1,
        "max_sections": DEFAULT_CONTEXT_MAX_SECTIONS,
        "max_chars": DEFAULT_CONTEXT_MAX_CHARS,
        "max_input_tokens": DEFAULT_CONTEXT_MAX_INPUT_TOKENS,
        "budget_class": execution_request.budget_class,
        "execution_request_id": str(execution_request.id),
        "proposal_revision_id": str(execution_request.proposal_revision_id),
        "plan_id": str(execution_request.plan_snapshot_id),
        "plan_generation": execution_request.plan_generation,
    }
    if code_input_manifest is not None:
        budget["execution_id"] = str(execution.id)
        budget["code_input_hash"] = code_input_manifest.input_hash
    if additional_budget:
        overlapping_keys = set(additional_budget).intersection(budget)
        if overlapping_keys:
            raise ValueError(
                "additional context budget cannot replace execution facts: "
                + ", ".join(sorted(overlapping_keys))
            )
        budget.update(additional_budget)
    if input_selection is not None:
        selection_budget = {
            "input_selection_id": str(input_selection.id),
            "input_selection_hash": input_selection.selection_hash,
            "input_selection_target_request_version": input_selection.target_request_version,
            "input_selection_plan_generation": input_selection.plan_generation,
            "input_selection_plan_content_hash": input_selection.plan_content_hash,
        }
        if set(selection_budget).intersection(additional_budget or {}):
            raise ValueError("additional context budget cannot replace input selection facts")
        budget.update(selection_budget)
    snapshot = work_context_revision.snapshot
    search_scope = _search_scope(
        snapshot.objective,
        tuple(item.text for item in snapshot.requested_outputs),
    )
    filter_refs = tuple(item.to_mapping() for item in snapshot.knowledge_refs)
    fact_scope_refs = (
        "planning_request",
        "execution_request",
        "execution",
        "worker_item",
        "proposal_revision",
        "plan_snapshot",
        "dependency_state",
    )
    fact_scope_refs += tuple(additional_fact_scope_refs)
    if input_selection is not None:
        fact_scope_refs += ("collaboration_release", "input_selection")
    if code_input_manifest is not None:
        fact_scope_refs += ("code_input_manifest",)
    if verification_manifest is not None:
        fact_scope_refs += ("verification_manifest",)
    expires_at = _expires_at(created_at, ttl_seconds)
    fingerprint = _request_fingerprint(
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=execution.id,
        worker_item_id=worker_item.id,
        work_context_revision_id=work_context_revision.revision_id,
        context_version=worker_item.context_version,
        search_scope=search_scope,
        filter_refs=filter_refs,
        fact_scope_refs=fact_scope_refs,
        budget=budget,
        expires_at=expires_at,
        execution_request_id=execution_request.id,
        proposal_revision_id=execution_request.proposal_revision_id,
        plan_id=execution_request.plan_snapshot_id,
        plan_generation=execution_request.plan_generation,
    )
    if manifest_schema_version == 1:
        effective_idempotency_key = idempotency_key or f"context:execution:{execution.id}:v1"
    elif manifest_schema_version == 2:
        assert input_selection is not None
        if idempotency_key is not None and (
            ":v2:" not in idempotency_key
            or input_selection.selection_hash not in idempotency_key
        ):
            raise ValueError(
                "v2 execution context idempotency key must include protocol and selection hash"
            )
        effective_idempotency_key = idempotency_key or (
            f"context:execution:{execution.id}:v2:context:{worker_item.context_version}:"
            f"revision:{work_context_revision.revision_id}:selection:{input_selection.selection_hash}"
        )
    else:
        input_identity = (
            code_input_manifest.input_hash
            if code_input_manifest is not None
            else verification_manifest.verification_basis_hash
            if verification_manifest is not None
            else None
        )
        assert input_identity is not None
        if idempotency_key is not None and (
            ":v3:" not in idempotency_key or input_identity not in idempotency_key
        ):
            raise ValueError(
                "v3 execution context idempotency key must include protocol and input hash"
            )
        effective_idempotency_key = idempotency_key or (
            f"context:execution:{execution.id}:v3:context:{worker_item.context_version}:"
            f"revision:{work_context_revision.revision_id}:input:{input_identity}"
        )
    return ContextAssemblyRequest.create(
        request_id=request_id or uuid4(),
        workspace_id=execution.workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=execution.id,
        worker_item_id=worker_item.id,
        work_context_revision_id=work_context_revision.revision_id,
        context_version=worker_item.context_version,
        knowledge_search_scope=search_scope,
        knowledge_filter_refs=filter_refs,
        fact_scope_refs=fact_scope_refs,
        budget=budget,
        expires_at=expires_at,
        request_fingerprint=fingerprint,
        idempotency_key=effective_idempotency_key,
        created_at=created_at,
        manifest_schema_version=manifest_schema_version,
        input_selection_id=(input_selection.id if input_selection is not None else None),
        input_selection_hash=(
            input_selection.selection_hash if input_selection is not None else None
        ),
        result_input_budget=result_input_budget,
        code_input_manifest=code_input_manifest,
        verification_manifest=verification_manifest,
    )


def execution_context_manifest_schema_version(
    uow: ControlPlaneUnitOfWork,
    execution: Execution,
) -> int:
    """Prefer an existing V3 package before the legacy selection package."""

    authorization = resolve_execution_context_authorization(uow, execution)
    if authorization is None:
        return 1
    request = authorization.request
    if request.result_derivation_kind == "verification":
        return 3
    if request.input_selection_id is None:
        return 1
    v3 = uow.context_assemblies.get_for_consumer(
        ContextPurpose.EXECUTION,
        execution.id,
        execution.workspace_id,
        manifest_schema_version=3,
    )
    return 3 if v3 is not None else 2


def validate_execution_context_selection(
    *,
    execution_request: ExecutionRequest,
    release: RuntimeRelease,
    selection: CollaborationInputSelectionV1,
) -> None:
    """Ensure an execution context can only use the Selection in its Release."""

    mismatches: list[str] = []
    if selection.workspace_id != execution_request.workspace_id:
        mismatches.append("selection workspace")
    if release.workspace_id != execution_request.workspace_id:
        mismatches.append("release workspace")
    if selection.target_execution_request_id != execution_request.id:
        mismatches.append("selection target request")
    if release.execution_request_id != execution_request.id:
        mismatches.append("release target request")
    if selection.target_worker_item_id != execution_request.worker_item_id:
        mismatches.append("selection target WorkerItem")
    if selection.root_task_id != execution_request.root_task_id:
        mismatches.append("selection root task")
    if selection.plan_snapshot_id != execution_request.plan_snapshot_id:
        mismatches.append("selection plan")
    if selection.plan_generation != execution_request.plan_generation:
        mismatches.append("selection plan generation")
    if selection.plan_content_hash != release.plan_content_hash:
        mismatches.append("selection plan hash")
    if release.plan_snapshot_id != execution_request.plan_snapshot_id:
        mismatches.append("release plan")
    if release.plan_generation != execution_request.plan_generation:
        mismatches.append("release plan generation")
    if selection.run_id != release.run_id:
        mismatches.append("selection collaboration run")
    if selection.target_request_version != release.request_version_at_release:
        mismatches.append("selection release request version")
    if selection.target_request_version > execution_request.version:
        mismatches.append("selection request version is newer than target")
    if execution_request.input_selection_id != selection.id:
        mismatches.append("execution request selection id")
    if execution_request.input_selection_hash != selection.selection_hash:
        mismatches.append("execution request selection hash")
    if release.input_selection_id != selection.id:
        mismatches.append("release selection id")
    if release.input_selection_hash != selection.selection_hash:
        mismatches.append("release selection hash")
    if mismatches:
        raise ValueError(
            "execution context selection scope mismatch: " + ", ".join(mismatches)
        )


def _validate_selection_target(
    *,
    execution_request: ExecutionRequest,
    worker_item: WorkerItem,
    selection: CollaborationInputSelectionV1,
) -> None:
    if selection.workspace_id != execution_request.workspace_id:
        raise ValueError("input selection workspace does not match execution")
    if selection.target_execution_request_id != execution_request.id:
        raise ValueError("input selection target request does not match execution")
    if selection.target_worker_item_id != worker_item.id:
        raise ValueError("input selection target WorkerItem does not match execution")
    if selection.root_task_id != execution_request.root_task_id:
        raise ValueError("input selection root task does not match execution")
    if selection.plan_snapshot_id != execution_request.plan_snapshot_id:
        raise ValueError("input selection plan does not match execution")
    if selection.plan_generation != execution_request.plan_generation:
        raise ValueError("input selection plan generation does not match execution")
    if execution_request.input_selection_id != selection.id:
        raise ValueError("execution request is not bound to the input selection")
    if execution_request.input_selection_hash != selection.selection_hash:
        raise ValueError("execution request input selection hash does not match")


def rebind_planning_context_assembly_request(
    request: ContextAssemblyRequest,
    *,
    work_context_revision: WorkContextRevision,
) -> ContextAssemblyRequest:
    """Rebind a queued planning assembly to a newer immutable context revision."""
    if request.purpose is not ContextPurpose.PLANNING_RUN:
        raise ValueError("only planning assemblies can be rebound")
    snapshot = work_context_revision.snapshot
    search_scope = _search_scope(
        snapshot.objective,
        tuple(item.text for item in snapshot.requested_outputs),
    )
    filter_refs = tuple(item.to_mapping() for item in snapshot.knowledge_refs)
    fingerprint = _request_fingerprint(
        purpose=request.purpose,
        consumer_type=request.consumer_type,
        consumer_id=request.consumer_id,
        worker_item_id=request.worker_item_id,
        work_context_revision_id=work_context_revision.revision_id,
        context_version=request.context_version,
        search_scope=search_scope,
        filter_refs=filter_refs,
        fact_scope_refs=tuple(str(item) for item in request.fact_scope_refs),
        budget=request.budget,
        expires_at=request.expires_at,
    )
    return replace(
        request,
        work_context_revision_id=work_context_revision.revision_id,
        knowledge_search_scope=search_scope,
        knowledge_filter_refs=filter_refs,
        request_fingerprint=fingerprint,
        status=ContextAssemblyStatus.QUEUED,
        lease_owner=None,
        lease_expires_at=None,
        next_attempt_at=None,
        error_code=None,
    )


def _search_scope(objective: str, requested_outputs: tuple[str, ...]) -> dict[str, object]:
    query = _normalize_query((objective, *requested_outputs))
    return {
        "query": query,
        "tags": (),
        "source_types": (),
        "status": "active",
        "limit": 20,
    }


def _normalize_query(parts: tuple[str, ...]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _expires_at(created_at: datetime, ttl_seconds: int) -> datetime:
    if ttl_seconds < 1:
        raise ValueError("context TTL must be positive")
    return created_at + timedelta(seconds=ttl_seconds)


def _request_fingerprint(
    *,
    purpose: ContextPurpose,
    consumer_type: ContextConsumerType,
    consumer_id: UUID,
    worker_item_id: UUID | None,
    work_context_revision_id: UUID,
    context_version: int | None,
    search_scope: Mapping[str, object],
    filter_refs: tuple[Mapping[str, object], ...],
    fact_scope_refs: tuple[str, ...],
    budget: Mapping[str, object],
    expires_at: datetime,
    **consumer_facts: object,
) -> str:
    canonical = {
        "purpose": purpose.value,
        "consumer_type": consumer_type.value,
        "consumer_id": str(consumer_id),
        "worker_item_id": str(worker_item_id) if worker_item_id else None,
        "work_context_revision_id": str(work_context_revision_id),
        "context_version": context_version,
        "knowledge_search_scope": search_scope,
        "knowledge_filter_refs": filter_refs,
        "fact_scope_refs": fact_scope_refs,
        "budget": budget,
        "expires_at": expires_at.isoformat(),
        "consumer_facts": consumer_facts,
    }
    encoded = json.dumps(
        _json_ready(canonical),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_ready(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value
