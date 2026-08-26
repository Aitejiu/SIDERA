from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextAssemblyStatus,
    ContextConsumerType,
    ContextPurpose,
)
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.planning import (
    PlanningBriefRevision,
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
) -> ContextAssemblyRequest:
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
) -> ContextAssemblyRequest:
    if execution.worker_item_id != worker_item.id:
        raise ValueError("execution and WorkerItem do not match")
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
    return ContextAssemblyRequest.create(
        request_id=uuid4(),
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
        idempotency_key=f"context:execution:{execution.id}:v1",
        created_at=created_at,
    )


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
