from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn, cast

from sidera_control_plane.domain.artifacts import ArtifactType
from sidera_control_plane.domain.collaboration import (
    CollaborationMode,
    CollaborationPolicyRequestV1,
    CollaborationPolicyRequestV2,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.modules import PlannerAdapterCapabilityProfile
from sidera_control_plane.domain.planning import PlanningRequest, PlanningRequestKind
from sidera_control_plane.domain.planning_profiles import PlanningProfileSnapshot
from sidera_control_plane.domain.planning_proposals import (
    PlanningProposalPayload,
    PlanningProposalPayloadV4,
    PlanningProposalPayloadV5,
    PlanningProposalPayloadV6,
)
from sidera_control_plane.domain.planning_revision_proposals import (
    PlanningRevisionProposalPayloadV3,
    PlanningRevisionProposalPayloadV4,
    PlanningRevisionProposalPayloadV5,
    PlanningRevisionProposalPayloadV6,
)
from sidera_control_plane.domain.plans import DependencyKind, TaskPlanSnapshot
from sidera_control_plane.domain.result_inputs import (
    InputGroupInvalid,
    InputGroupV1,
    InputWaitCycle,
    validate_input_wait_graph,
)
from sidera_control_plane.domain.result_integration import validate_integration_spec

ProposalPayload = (
    PlanningProposalPayload
    | PlanningProposalPayloadV4
    | PlanningProposalPayloadV5
    | PlanningProposalPayloadV6
    | PlanningRevisionProposalPayloadV3
    | PlanningRevisionProposalPayloadV4
    | PlanningRevisionProposalPayloadV5
    | PlanningRevisionProposalPayloadV6
)


class ProposalValidationError(ValidationError):
    def __init__(
        self, code: str, detail: str, offending_keys: tuple[str, ...] = ()
    ) -> None:
        ValidationError.__init__(
            self,
            f"{code}: {detail}",
            details={"code": code, "offending_keys": list(offending_keys)},
        )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "offending_keys", offending_keys)


def validate_proposal(
    payload: ProposalPayload | Mapping[str, object],
    *,
    profile_snapshot: PlanningProfileSnapshot,
    brief_revision: int,
    request: PlanningRequest | None = None,
    capability_snapshot: PlannerAdapterCapabilityProfile | None = None,
    base_snapshot: TaskPlanSnapshot | None = None,
    kind: PlanningRequestKind | None = None,
) -> ProposalPayload:
    typed_payload = _typed_payload(payload)
    explicit_revision = request is not None or kind is not None
    expected_kind = request.kind if request is not None else kind
    if expected_kind is None:
        expected_kind = typed_payload.kind
    if expected_kind is PlanningRequestKind.REVISION:
        if not isinstance(
            typed_payload,
            (
                PlanningRevisionProposalPayloadV3,
                PlanningRevisionProposalPayloadV4,
                PlanningRevisionProposalPayloadV5,
                PlanningRevisionProposalPayloadV6,
            ),
        ):
            if not explicit_revision:
                expected_kind = PlanningRequestKind.CREATE
            else:
                _fail(
                    "proposal_schema_invalid",
                    "revision proposal schema v3, v4, v5, or v6 is required",
                )
        else:
            _validate_revision_proposal(
                typed_payload,
                profile_snapshot=profile_snapshot,
                capability_snapshot=capability_snapshot,
                brief_revision=brief_revision,
                request=request,
                base_snapshot=base_snapshot,
            )
            return typed_payload
    if not isinstance(
        typed_payload,
        (
            PlanningProposalPayload,
            PlanningProposalPayloadV4,
            PlanningProposalPayloadV5,
            PlanningProposalPayloadV6,
        ),
    ):
        _fail(
            "proposal_schema_invalid",
            "create proposal schema v2 is required",
        )
    is_v4 = isinstance(typed_payload, PlanningProposalPayloadV4)
    is_v5 = isinstance(typed_payload, PlanningProposalPayloadV5)
    is_v6 = isinstance(typed_payload, PlanningProposalPayloadV6)
    expected_schema_version = 6 if is_v6 else 5 if is_v5 else 4 if is_v4 else 2
    if typed_payload.schema_version != expected_schema_version:
        _fail(
            "proposal_schema_invalid",
            f"proposal schema version {expected_schema_version} is required",
        )
    if profile_snapshot.proposal_schema_version != typed_payload.schema_version:
        _fail("proposal_schema_invalid", "proposal schema is not selected by the profile")
    if brief_revision < 1:
        _fail("proposal_schema_invalid", "brief revision must be positive")
    if typed_payload.kind is not PlanningRequestKind.CREATE:
        _fail("proposal_revision_operation_forbidden", "R5.1 accepts create proposals only")
    if (
        not is_v4
        and not is_v5
        and not is_v6
        and typed_payload.base_descriptor is not None
    ):
        _fail("proposal_revision_operation_forbidden", "base descriptor is reserved for revision proposals")
    if typed_payload.impact_policy.revision_operation is not None:
        _fail("proposal_revision_operation_forbidden", "revision operation is not supported")
    if typed_payload.impact_policy.manual_gate:
        _fail("proposal_manual_gate_unsupported", "manual approval gates are not supported in R5.1")

    validation_payload = (
        typed_payload.to_legacy() if is_v4 or is_v5 or is_v6 else typed_payload
    )
    _validate_unique_keys(validation_payload)
    _validate_references(validation_payload)
    _validate_cycles(
        validation_payload,
        input_groups=(
            typed_payload.collaboration_policy_request.input_groups
            if is_v5 or is_v6
            else None
        ),
    )
    _validate_capabilities(validation_payload, profile_snapshot)
    _validate_budget(validation_payload, profile_snapshot)
    _validate_intents(validation_payload)
    if is_v4:
        _validate_collaboration_request(typed_payload, profile_snapshot)
    elif is_v5 or is_v6:
        _validate_collaboration_request_v2(typed_payload, profile_snapshot)
    if is_v6:
        validate_integration_spec(
            typed_payload.integration_spec,
            frozenset(item.key for item in typed_payload.worker_items),
            frozenset(
                group.group_key
                for group in typed_payload.collaboration_policy_request.input_groups
            ),
        )
    return typed_payload


def _typed_payload(
    payload: ProposalPayload | Mapping[str, object],
) -> ProposalPayload:
    if isinstance(
        payload,
        (
            PlanningProposalPayload,
            PlanningProposalPayloadV4,
            PlanningProposalPayloadV5,
            PlanningProposalPayloadV6,
            PlanningRevisionProposalPayloadV3,
            PlanningRevisionProposalPayloadV4,
            PlanningRevisionProposalPayloadV5,
            PlanningRevisionProposalPayloadV6,
        ),
    ):
        return payload
    if not isinstance(payload, Mapping):
        _fail("proposal_schema_invalid", "proposal payload must be a typed mapping")
    schema_version = payload.get("schema_version")
    kind = payload.get("kind")
    if schema_version == 4 and kind == PlanningRequestKind.CREATE.value:
        try:
            return PlanningProposalPayloadV4.from_mapping(payload)
        except ValidationError as exc:
            _fail("proposal_schema_invalid", str(exc))
    if schema_version == 5 and kind == PlanningRequestKind.CREATE.value:
        try:
            return PlanningProposalPayloadV5.from_mapping(payload)
        except ValidationError as exc:
            _fail("proposal_schema_invalid", str(exc))
    if schema_version == 6 and kind == PlanningRequestKind.CREATE.value:
        try:
            return PlanningProposalPayloadV6.from_mapping(payload)
        except ValidationError as exc:
            _fail("proposal_schema_invalid", str(exc))
    if schema_version == 4 or schema_version == 3 or kind == PlanningRequestKind.REVISION.value:
        try:
            if schema_version == 4:
                return PlanningRevisionProposalPayloadV4.from_mapping(payload)
            if schema_version == 5:
                return PlanningRevisionProposalPayloadV5.from_mapping(payload)
            if schema_version == 6:
                return PlanningRevisionProposalPayloadV6.from_mapping(payload)
            return PlanningRevisionProposalPayloadV3.from_mapping(payload)
        except ValidationError as exc:
            _fail("proposal_schema_invalid", str(exc))
    if _contains_concrete_identity(
        payload, {"root_task_id", "task_id", "worker_item_id", "pinned_worker_id"}
    ):
        _fail("proposal_worker_binding_forbidden", "proposal cannot contain concrete task or worker identities")
    try:
        return PlanningProposalPayload.from_mapping(payload)
    except ValidationError as exc:
        _fail("proposal_schema_invalid", str(exc))
    raise AssertionError("proposal payload validation did not return")


def _validate_revision_proposal(
    payload: (
        PlanningRevisionProposalPayloadV3
        | PlanningRevisionProposalPayloadV4
        | PlanningRevisionProposalPayloadV5
        | PlanningRevisionProposalPayloadV6
    ),
    *,
    profile_snapshot: PlanningProfileSnapshot,
    capability_snapshot: PlannerAdapterCapabilityProfile | None,
    brief_revision: int,
    request: PlanningRequest | None,
    base_snapshot: TaskPlanSnapshot | None,
) -> None:
    if profile_snapshot.proposal_schema_version != payload.schema_version:
        _fail(
            "proposal_schema_invalid",
            f"revision proposal schema v{payload.schema_version} is not selected by the profile",
        )
    capabilities = capability_snapshot or profile_snapshot.planner_capabilities
    if not isinstance(capabilities, PlannerAdapterCapabilityProfile):
        _fail("proposal_schema_invalid", "planner capability snapshot is invalid")
    if payload.schema_version not in capabilities.proposal_schema_versions:
        _fail(
            "proposal_schema_invalid",
            f"revision proposal schema v{payload.schema_version} is not supported by the adapter",
        )
    if brief_revision < 1:
        _fail("proposal_schema_invalid", "brief revision must be positive")
    if request is not None:
        if request.kind is not PlanningRequestKind.REVISION:
            _fail("proposal_revision_operation_forbidden", "request is not a revision")
        if request.base_descriptor != payload.base_descriptor:
            _fail(
                "proposal_base_mismatch",
                "revision proposal base descriptor does not match the request",
            )
    try:
        if base_snapshot is not None:
            payload.validate_against_base(base_snapshot)
        else:
            payload.validate()
    except ValidationError as exc:
        detail = str(exc)
        lowered = detail.lower()
        if "base" in lowered:
            code = "proposal_base_mismatch"
        elif "cycle" in lowered:
            code = "proposal_cycle"
        elif "identity" in lowered or "cover" in lowered:
            code = "proposal_identity_invalid"
        else:
            code = "proposal_schema_invalid"
        _fail(code, detail)
    _validate_capabilities(payload, profile_snapshot)
    _validate_budget(payload, profile_snapshot)
    if isinstance(payload, PlanningRevisionProposalPayloadV4):
        _validate_collaboration_request(payload, profile_snapshot)
    elif isinstance(
        payload,
        (PlanningRevisionProposalPayloadV5, PlanningRevisionProposalPayloadV6),
    ):
        _validate_collaboration_request_v2(payload, profile_snapshot)


def _validate_collaboration_request(
    payload: PlanningProposalPayloadV4 | PlanningRevisionProposalPayloadV4,
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    request = payload.collaboration_policy_request
    if not isinstance(request, CollaborationPolicyRequestV1):
        _fail(
            "proposal_schema_invalid",
            "collaboration policy request is invalid",
        )
    request.validate_for_subtasks(frozenset(item.key for item in payload.subtasks))
    if request.mode is CollaborationMode.DISABLED:
        return
    binding = profile_snapshot.collaboration_runtime_binding
    if binding is None:
        _fail(
            "proposal_collaboration_binding_missing",
            "enabled collaboration policy requires a pinned runtime binding",
        )
    supported = set(binding.runtime_capability_snapshot.strategy_ids)
    requested = {request.default_policy.strategy_id.value}
    requested.update(item.strategy_id.value for item in request.subtask_overrides)
    unsupported = sorted(requested - supported)
    if unsupported:
        _fail(
            "proposal_collaboration_strategy_unsupported",
            f"runtime does not support collaboration strategy: {', '.join(unsupported)}",
        )


def _validate_collaboration_request_v2(
    payload: (
        PlanningProposalPayloadV5
        | PlanningProposalPayloadV6
        | PlanningRevisionProposalPayloadV5
        | PlanningRevisionProposalPayloadV6
    ),
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    request = payload.collaboration_policy_request
    if not isinstance(request, CollaborationPolicyRequestV2):
        _fail(
            "proposal_schema_invalid",
            "collaboration policy request v2 is invalid",
        )
    request.validate_for_subtasks(frozenset(item.key for item in payload.subtasks))
    if request.mode is CollaborationMode.DISABLED:
        return
    binding = profile_snapshot.collaboration_runtime_binding
    if binding is None:
        _fail(
            "proposal_collaboration_binding_missing",
            "enabled collaboration policy requires a pinned runtime binding",
        )
    if not binding.runtime_capability_snapshot.supports_input_groups_v1():
        _fail(
            "proposal_collaboration_capability_unsupported",
            "runtime does not support input_groups_v1 with projection and decision schema v2",
        )
    supported = set(binding.runtime_capability_snapshot.strategy_ids)
    requested = {request.default_policy.strategy_id.value}
    requested.update(item.strategy_id.value for item in request.subtask_overrides)
    unsupported = sorted(requested - supported)
    if unsupported:
        _fail(
            "proposal_collaboration_strategy_unsupported",
            f"runtime does not support collaboration strategy: {', '.join(unsupported)}",
        )
    workers_by_key = {item.key: item for item in payload.worker_items}
    overrides = {item.subtask_key: item for item in request.subtask_overrides}
    for group in request.input_groups:
        consumer = workers_by_key[group.consumer_worker_item_key]
        policy = overrides.get(consumer.subtask_key)
        enabled = request.default_policy.enabled if policy is None else policy.enabled
        if not enabled:
            _fail(
                "proposal_collaboration_strategy_unsupported",
                "input group consumer requires an enabled collaboration subtask",
                (consumer.key,),
            )


def _validate_unique_keys(payload: PlanningProposalPayload) -> None:
    _unique(tuple(item.key for item in payload.subtasks), "subtask")
    _unique(tuple(item.key for item in payload.worker_items), "worker item")
    _unique(tuple(item.key for item in payload.subtask_milestones), "milestone")
    intent_keys = tuple(item.worker_item_key for item in payload.execution_intents)
    _unique(intent_keys, "execution intent")
    if not payload.subtasks:
        _fail("proposal_schema_invalid", "proposal must contain at least one subtask")
    if not payload.worker_items:
        _fail("proposal_schema_invalid", "proposal must contain at least one worker item")

    subtask_keys = {item.key for item in payload.subtasks}
    workers_by_subtask: dict[str, int] = {key: 0 for key in subtask_keys}
    for item in payload.worker_items:
        if item.subtask_key not in subtask_keys:
            _fail(
                "proposal_reference_invalid",
                "worker item references an unknown subtask",
                (item.key, item.subtask_key),
            )
        workers_by_subtask[item.subtask_key] += 1
    missing_workers = tuple(key for key, count in workers_by_subtask.items() if count == 0)
    if missing_workers:
        _fail(
            "proposal_reference_invalid",
            "every executable subtask needs a worker item",
            missing_workers,
        )


def _validate_references(payload: PlanningProposalPayload) -> None:
    subtask_keys = {item.key for item in payload.subtasks}
    worker_keys = {item.key for item in payload.worker_items}
    milestone_keys = {item.key for item in payload.subtask_milestones}
    for item in payload.subtasks:
        if item.milestone_key is not None and item.milestone_key not in milestone_keys:
            _fail("proposal_reference_invalid", "subtask milestone reference is unknown", (item.key, item.milestone_key))
        for ref in item.acceptance_refs:
            _validate_evidence_ref(ref, item.key)
    for ref in payload.root_task.acceptance_refs:
        _validate_evidence_ref(ref, "root")
    for milestone in payload.subtask_milestones:
        for key in milestone.subtask_keys:
            if key not in subtask_keys:
                _fail("proposal_reference_invalid", "milestone references an unknown subtask", (milestone.key, key))
        for ref in milestone.acceptance_refs:
            _validate_evidence_ref(ref, milestone.key)
    for dependency in payload.subtask_dependencies:
        _edge_reference(
            dependency.upstream_subtask_key,
            dependency.downstream_subtask_key,
            subtask_keys,
            "subtask dependency",
        )
        for ref in dependency.required_evidence_refs:
            _validate_evidence_ref(ref, dependency.downstream_subtask_key)
    for dependency in payload.worker_item_dependencies:
        _edge_reference(
            dependency.upstream_worker_item_key,
            dependency.downstream_worker_item_key,
            worker_keys,
            "worker item dependency",
        )
        if dependency.kind not in {"hard", "informational"}:
            _fail("proposal_schema_invalid", "worker dependency kind is unsupported", (dependency.kind,))
        for ref in dependency.required_artifact_refs:
            _validate_artifact_ref(ref, dependency.downstream_worker_item_key)
    for intent in payload.execution_intents:
        if intent.worker_item_key not in worker_keys:
            _fail("proposal_intent_mismatch", "execution intent references an unknown worker item", (intent.worker_item_key,))
        for ref in (*intent.expected_artifact_refs,):
            _validate_artifact_ref(ref, intent.worker_item_key)
        for ref in intent.required_evidence_refs:
            _validate_evidence_ref(ref, intent.worker_item_key)
        for dependency in intent.activation_dependencies:
            _edge_reference(
                dependency.upstream_worker_item_key,
                dependency.downstream_worker_item_key,
                worker_keys,
                "activation dependency",
            )
            if dependency.kind not in {"hard", "informational"}:
                _fail("proposal_schema_invalid", "activation dependency kind is unsupported", (dependency.kind,))
            for ref in dependency.required_artifact_refs:
                _validate_artifact_ref(ref, dependency.downstream_worker_item_key)


def _validate_cycles(
    payload: PlanningProposalPayload,
    *,
    input_groups: tuple[InputGroupV1, ...] | None = None,
) -> None:
    _assert_acyclic(
        tuple(item.key for item in payload.subtasks),
        tuple(
            (item.upstream_subtask_key, item.downstream_subtask_key)
            for item in payload.subtask_dependencies
            if item.kind.value == DependencyKind.HARD.value
        ),
        "subtask hard dependency graph",
    )
    _assert_acyclic(
        tuple(item.key for item in payload.worker_items),
        tuple(
            (item.upstream_worker_item_key, item.downstream_worker_item_key)
            for item in payload.worker_item_dependencies
            if item.kind == DependencyKind.HARD.value
        ),
        "worker item hard dependency graph",
    )
    activation_edges = tuple(
        (dependency.upstream_worker_item_key, dependency.downstream_worker_item_key)
        for intent in payload.execution_intents
        for dependency in intent.activation_dependencies
        if dependency.kind == DependencyKind.HARD.value
    )
    _assert_acyclic(
        tuple(item.key for item in payload.worker_items),
        activation_edges,
        "activation dependency graph",
    )
    _validate_input_group_cycles(payload, input_groups=input_groups)


def _validate_input_group_cycles(
    payload: PlanningProposalPayload,
    *,
    input_groups: tuple[InputGroupV1, ...] | None = None,
) -> None:
    """Add potential input edges without changing legacy policy behavior."""

    if input_groups is None:
        policy_request = getattr(payload, "collaboration_policy_request", None)
        raw_groups = getattr(policy_request, "input_groups", ())
    else:
        raw_groups = input_groups
    if not raw_groups:
        return
    input_groups = cast(tuple[InputGroupV1, ...], raw_groups)
    grouped_workers: dict[str, list[str]] = {}
    for item in payload.worker_items:
        grouped_workers.setdefault(item.subtask_key, []).append(item.key)
    workers_by_subtask = {
        key: tuple(values) for key, values in grouped_workers.items()
    }

    hard_edges: set[tuple[str, str]] = set()
    for dependency in payload.worker_item_dependencies:
        if dependency.kind == DependencyKind.HARD.value:
            hard_edges.add(
                (
                    dependency.upstream_worker_item_key,
                    dependency.downstream_worker_item_key,
                )
            )
    for intent in payload.execution_intents:
        for dependency in intent.activation_dependencies:
            if dependency.kind == DependencyKind.HARD.value:
                hard_edges.add(
                    (
                        dependency.upstream_worker_item_key,
                        dependency.downstream_worker_item_key,
                    )
                )
    for dependency in payload.subtask_dependencies:
        if dependency.kind.value != DependencyKind.HARD.value:
            continue
        for upstream in workers_by_subtask.get(dependency.upstream_subtask_key, ()):
            for downstream in workers_by_subtask.get(
                dependency.downstream_subtask_key, ()
            ):
                hard_edges.add((upstream, downstream))

    try:
        validate_input_wait_graph(
            worker_keys=frozenset(item.key for item in payload.worker_items),
            hard_edges=tuple(sorted(hard_edges)),
            input_groups=input_groups,
        )
    except InputWaitCycle as exc:
        raw_nodes = exc.details.get("cycle_nodes", ())
        cycle_nodes = raw_nodes if isinstance(raw_nodes, (list, tuple)) else ()
        nodes = tuple(value for value in cycle_nodes if isinstance(value, str))
        _fail("input_wait_cycle", str(exc), nodes)
    except InputGroupInvalid as exc:
        _fail("input_group_invalid", str(exc))


def _validate_capabilities(
    payload: ProposalPayload,
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    allowed = set(profile_snapshot.allowed_capabilities)
    forbidden_adapters = set(profile_snapshot.adapter_forbidden)
    for item in payload.worker_items:
        if not set(item.capability_requirements).issubset(allowed):
            _fail("proposal_capability_forbidden", "worker item requests a capability outside the profile", (item.key,))
        if item.pinned_worker_id is not None:
            _fail("proposal_worker_binding_forbidden", "concrete worker binding is not allowed", (item.key,))
    for intent in payload.execution_intents:
        if not set(intent.capability_requirements).issubset(allowed):
            _fail("proposal_capability_forbidden", "execution intent requests a capability outside the profile", (intent.worker_item_key,))
        if set(intent.adapter_preferences).intersection(forbidden_adapters):
            _fail("proposal_capability_forbidden", "execution intent requests a forbidden adapter", (intent.worker_item_key,))


def _validate_budget(
    payload: ProposalPayload,
    profile_snapshot: PlanningProfileSnapshot,
) -> None:
    budget = profile_snapshot.budget
    summary = payload.decision_summary
    if summary.estimated_tokens > budget.max_tokens:
        _fail("proposal_budget_exceeded", "estimated tokens exceed the profile budget")
    if summary.estimated_cost_microunits > budget.max_cost_microunits:
        _fail("proposal_budget_exceeded", "estimated cost exceeds the profile budget")
    if summary.estimated_runtime_seconds > budget.max_runtime_seconds:
        _fail("proposal_budget_exceeded", "estimated runtime exceeds the profile budget")
    if summary.estimated_concurrency > budget.max_concurrency:
        _fail("proposal_budget_exceeded", "estimated concurrency exceeds the profile budget")
    if 1 + len(payload.subtasks) + len(payload.worker_items) > budget.max_nodes:
        _fail("proposal_budget_exceeded", "proposal node count exceeds the profile budget")
    for item in payload.worker_items:
        if item.concurrency_limit is not None and item.concurrency_limit > budget.max_concurrency:
            _fail("proposal_budget_exceeded", "worker item concurrency exceeds the profile budget", (item.key,))
    for intent in payload.execution_intents:
        if intent.max_attempts > profile_snapshot.execution_max_attempts:
            _fail("proposal_budget_exceeded", "execution attempts exceed the profile policy", (intent.worker_item_key,))
        for seconds in (intent.deadline_seconds, intent.not_before_seconds):
            if seconds is not None and seconds > budget.max_runtime_seconds:
                _fail("proposal_budget_exceeded", "execution timing exceeds the profile budget", (intent.worker_item_key,))


def _validate_intents(payload: PlanningProposalPayload) -> None:
    workers = {item.key: item for item in payload.worker_items}
    intent_keys = {item.worker_item_key for item in payload.execution_intents}
    worker_keys = set(workers)
    if intent_keys != worker_keys:
        _fail("proposal_intent_mismatch", "every WorkerItem must have exactly one execution intent")
    for intent in payload.execution_intents:
        worker = workers[intent.worker_item_key]
        if set(intent.capability_requirements) != set(worker.capability_requirements):
            _fail("proposal_intent_mismatch", "intent capabilities do not match the WorkerItem", (intent.worker_item_key,))


def _validate_artifact_ref(value: str, key: str) -> None:
    prefix, separator, artifact_type = value.partition(":")
    if prefix != "artifact_type" or not separator or artifact_type not in {item.value for item in ArtifactType}:
        _fail("proposal_reference_invalid", "artifact reference must use artifact_type:<ArtifactType>", (key,))


def _validate_evidence_ref(value: str, key: str) -> None:
    prefix, separator, criterion = value.partition(":")
    if prefix != "acceptance" or not separator or not criterion.strip():
        _fail("proposal_reference_invalid", "evidence reference must use acceptance:<criterion-ref>", (key,))


def _edge_reference(upstream: str, downstream: str, keys: set[str], label: str) -> None:
    if upstream == downstream:
        _fail("proposal_cycle", f"{label} cannot point to itself", (upstream,))
    if upstream not in keys or downstream not in keys:
        _fail("proposal_reference_invalid", f"{label} references an unknown node", (upstream, downstream))


def _assert_acyclic(keys: tuple[str, ...], edges: tuple[tuple[str, str], ...], label: str) -> None:
    indegree = {key: 0 for key in keys}
    adjacency: dict[str, list[str]] = {key: [] for key in keys}
    for upstream, downstream in edges:
        if upstream not in indegree or downstream not in indegree:
            _fail("proposal_reference_invalid", f"{label} references an unknown node", (upstream, downstream))
        adjacency[upstream].append(downstream)
        indegree[downstream] += 1
    queue = [key for key in keys if indegree[key] == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for downstream in adjacency[current]:
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
    if visited != len(keys):
        _fail("proposal_cycle", f"{label} contains a cycle", tuple(key for key, count in indegree.items() if count > 0))


def _unique(keys: tuple[str, ...], label: str) -> None:
    if len(keys) != len(set(keys)):
        duplicates = tuple(sorted({key for key in keys if keys.count(key) > 1}))
        _fail("proposal_schema_invalid", f"{label} keys must be unique", duplicates)


def _contains_concrete_identity(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                isinstance(key, str)
                and key in keys
                and item is not None
            )
            or _contains_concrete_identity(item, keys)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_concrete_identity(item, keys) for item in value)
    return False


def _fail(code: str, detail: str, keys: tuple[str, ...] = ()) -> NoReturn:
    raise ProposalValidationError(code, detail, keys)
