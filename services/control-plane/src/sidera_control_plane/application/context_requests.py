from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.domain.collaboration import RuntimeRelease
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextConsumerType,
    ContextPurpose,
    ResultInputBudgetV1,
)
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.execution_requests import ExecutionRequest
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
) -> ContextAssemblyRequest:
    if execution.worker_item_id != worker_item.id:
        raise ValueError("execution and WorkerItem do not match")
    if manifest_schema_version == 1:
        if input_selection is not None or result_input_budget is not None:
            raise ValueError("v1 execution context cannot carry an input selection")
    elif manifest_schema_version == 2:
        if not isinstance(input_selection, CollaborationInputSelectionV1):
            raise ValueError("v2 execution context requires an input selection")
        _validate_selection_target(
            execution_request=execution_request,
            worker_item=worker_item,
            selection=input_selection,
        )
        result_input_budget = result_input_budget or ResultInputBudgetV1()
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
    else:
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
    )


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
