from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sidera_control_plane.domain.collaboration import CollaborationStrategy
from sidera_control_plane.domain.collaboration_runtime import (
    ReleaseExecutionRequestIntentV1,
    ReleaseExecutionRequestIntentV2,
    RuntimeDecision,
    RuntimeDecisionOutcome,
    RuntimeDecisionV1,
    RuntimeDecisionV2,
    RuntimeEnvelopeError,
    RuntimeProjection,
    RuntimeProjectionV1,
    RuntimeProjectionV2,
    RuntimeResultInputEvaluation,
    canonical_json_bytes,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus
from sidera_control_plane.domain.input_selections import (
    InputSelectionGroupV1,
    InputSelectionSourceV1,
)
from sidera_control_plane.domain.planning_profiles import (
    COLLABORATION_MAX_DECISION_BYTES,
    COLLABORATION_MAX_INTENTS_PER_DECISION,
    COLLABORATION_MAX_STRATEGY_STATE_BYTES,
)
from sidera_control_plane.domain.result_inputs import (
    InputGroupEvaluation,
    InputGroupMode,
    InputGroupV1,
    ResultInputSource,
)

Clock = Callable[[], datetime]


class ReferenceCollaborationRuntime:
    """Deterministic reference implementation of the all_required strategy."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate(
        self,
        projection: RuntimeProjection,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> RuntimeDecision:
        self._verify_deadline(deadline)
        strategy_state = dict(projection.previous_strategy_state)
        if len(canonical_json_bytes(strategy_state)) > _limit(
            projection.policy,
            "max_strategy_state_bytes",
            COLLABORATION_MAX_STRATEGY_STATE_BYTES,
        ):
            raise RuntimeEnvelopeError("strategy_state exceeds the runtime limit")

        if isinstance(projection, RuntimeProjectionV2):
            return self._evaluate_v2(
                projection,
                operation_id=operation_id,
                strategy_state=strategy_state,
            )

        released = _released_request_ids(projection)
        candidates = _eligible_requests(projection, released)
        intents = tuple(
            ReleaseExecutionRequestIntentV1(
                ordinal=ordinal,
                target_execution_request_id=request_id,
                target_request_version=version,
                reason_code="all_required_eligible",
                summary="release an eligible execution request",
                idempotency_key=(
                    f"collaboration-release:{projection.run_id}:"
                    f"{projection.projection_id}:{request_id}:v{version}"
                ),
            )
            for ordinal, (request_id, version) in enumerate(candidates)
        )
        if len(intents) > _limit(
            projection.policy,
            "max_intents_per_decision",
            COLLABORATION_MAX_INTENTS_PER_DECISION,
        ):
            raise RuntimeEnvelopeError("decision exceeds the runtime intent limit")
        decision = RuntimeDecisionV1(
            schema_version=1,
            operation_id=operation_id,
            run_id=projection.run_id,
            projection_id=projection.projection_id,
            projection_hash=projection.content_hash,
            outcome=(
                RuntimeDecisionOutcome.INTENTS
                if intents
                else RuntimeDecisionOutcome.NO_CHANGE
            ),
            reason_code=("eligible_requests" if intents else "no_eligible_requests"),
            summary=(
                f"release {len(intents)} eligible execution request(s)"
                if intents
                else "no release-required request is eligible"
            ),
            strategy_state=strategy_state,
            intents=intents,
        )
        if len(decision.canonical_bytes()) > _limit(
            projection.policy,
            "max_decision_bytes",
            COLLABORATION_MAX_DECISION_BYTES,
        ):
            raise RuntimeEnvelopeError("decision exceeds the runtime response limit")
        return decision

    @staticmethod
    def _evaluate_v2(
        projection: RuntimeProjectionV2,
        *,
        operation_id: UUID,
        strategy_state: Mapping[str, object],
    ) -> RuntimeDecisionV2:
        released = _released_request_ids(projection)
        intents = _v2_intents(projection, released=released)
        if len(intents) > _limit(
            projection.policy,
            "max_intents_per_decision",
            COLLABORATION_MAX_INTENTS_PER_DECISION,
        ):
            raise RuntimeEnvelopeError("decision exceeds the runtime intent limit")
        decision = RuntimeDecisionV2(
            schema_version=2,
            operation_id=operation_id,
            run_id=projection.run_id,
            projection_id=projection.projection_id,
            projection_hash=projection.content_hash,
            outcome=(
                RuntimeDecisionOutcome.INTENTS
                if intents
                else RuntimeDecisionOutcome.NO_CHANGE
            ),
            reason_code=("eligible_requests" if intents else "no_eligible_requests"),
            summary=(
                f"release {len(intents)} eligible execution request(s)"
                if intents
                else "no release-required request is eligible"
            ),
            strategy_state=strategy_state,
            intents=intents,
        )
        if len(decision.canonical_bytes()) > _limit(
            projection.policy,
            "max_decision_bytes",
            COLLABORATION_MAX_DECISION_BYTES,
        ):
            raise RuntimeEnvelopeError("decision exceeds the runtime response limit")
        return decision

    def _verify_deadline(self, deadline: datetime) -> None:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise RuntimeEnvelopeError("deadline must be timezone-aware")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeEnvelopeError("runtime clock must be timezone-aware")
        if now.astimezone(UTC) > deadline.astimezone(UTC):
            raise RuntimeEnvelopeError("evaluation deadline has expired")


def _eligible_requests(
    projection: RuntimeProjectionV1,
    released: set[str],
) -> tuple[tuple[UUID, int], ...]:
    found: dict[UUID, int] = {}
    for raw_request in sorted(
        projection.execution_requests,
        key=lambda item: str(item.get("id", "")),
    ):
        request_id = _request_uuid(raw_request.get("id"))
        if request_id is None:
            continue
        if request_id in found:
            raise RuntimeEnvelopeError(
                "projection contains duplicate execution request"
            )
        if str(request_id) in released:
            continue
        if raw_request.get("eligible") is not True:
            continue
        if raw_request.get("collaboration_gate_mode") != "release_required":
            continue
        if raw_request.get("status") not in {
            ExecutionRequestStatus.BLOCKED.value,
            ExecutionRequestStatus.READY.value,
        }:
            continue
        if not _uses_all_required_policy(projection.policy, raw_request):
            continue
        version = raw_request.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            continue
        found[request_id] = version
    return tuple(sorted(found.items(), key=lambda item: str(item[0])))


def select_plan_order(
    *,
    group: InputGroupV1,
    evaluation: InputGroupEvaluation,
) -> tuple[str, ...]:
    """Select only ready sources in immutable plan-priority order."""

    if evaluation.group_key != group.group_key:
        raise RuntimeEnvelopeError("input group evaluation does not match its rule")
    if not evaluation.satisfied:
        return ()
    ready = {item.worker_item_key for item in evaluation.ready_sources}
    ordered = tuple(
        candidate.worker_item_key
        for candidate in group.candidates
        if candidate.worker_item_key in ready
    )
    if group.mode is InputGroupMode.ALL:
        return ordered
    if group.minimum_count is None:
        raise RuntimeEnvelopeError("at_least input group has no minimum_count")
    return ordered[: group.minimum_count]


def _v2_intents(
    projection: RuntimeProjectionV2,
    *,
    released: set[str],
) -> tuple[ReleaseExecutionRequestIntentV2, ...]:
    raw_groups = projection.policy.get("input_groups")
    if not isinstance(raw_groups, (list, tuple)):
        raise RuntimeEnvelopeError("Projection v2 input_groups are invalid")
    groups = {
        group.group_key: group
        for group in (InputGroupV1.from_mapping(item) for item in raw_groups)
    }
    evaluations_by_request: dict[UUID, list[RuntimeResultInputEvaluation]] = {}
    for item in projection.result_input_evaluations:
        evaluations_by_request.setdefault(
            item.consumer_execution_request_id, []
        ).append(item)

    intents: list[ReleaseExecutionRequestIntentV2] = []
    for request_id, version, raw_request in _eligible_request_rows(
        projection, released=released
    ):
        evaluations = evaluations_by_request.get(request_id, [])
        selections: list[InputSelectionGroupV1] = []
        if evaluations:
            for item in sorted(evaluations, key=lambda value: value.group_key):
                group = groups.get(item.group_key)
                if group is None:
                    raise RuntimeEnvelopeError(
                        "Projection v2 evaluation references an unknown input group"
                    )
                selected_keys = select_plan_order(
                    group=group,
                    evaluation=item.evaluation,
                )
                if not selected_keys:
                    selections = []
                    break
                selections.append(
                    _selection_group(
                        group=group,
                        evaluation=item.evaluation,
                        selected_keys=selected_keys,
                    )
                )
            if len(selections) != len(evaluations):
                continue
        elif not _uses_all_required_policy(projection.policy, raw_request):
            continue
        reason_code = (
            "input_groups_satisfied" if selections else "all_required_eligible"
        )
        intents.append(
            ReleaseExecutionRequestIntentV2(
                ordinal=len(intents),
                target_execution_request_id=request_id,
                target_request_version=version,
                reason_code=reason_code,
                summary=(
                    "release with fixed result input sources"
                    if selections
                    else "release an eligible execution request"
                ),
                idempotency_key=(
                    f"collaboration-release:{projection.run_id}:"
                    f"{projection.projection_id}:{request_id}:v{version}"
                ),
                input_group_selections=tuple(selections),
            )
        )
    return tuple(intents)


def _eligible_request_rows(
    projection: RuntimeProjectionV2,
    *,
    released: set[str],
) -> tuple[tuple[UUID, int, Mapping[str, object]], ...]:
    found: dict[UUID, tuple[int, Mapping[str, object]]] = {}
    for raw_request in sorted(
        projection.execution_requests,
        key=lambda item: str(item.get("id", "")),
    ):
        request_id = _request_uuid(raw_request.get("id"))
        if request_id is None:
            continue
        if request_id in found:
            raise RuntimeEnvelopeError(
                "projection contains duplicate execution request"
            )
        if str(request_id) in released:
            continue
        if raw_request.get("eligible") is not True:
            continue
        if raw_request.get("collaboration_gate_mode") != "release_required":
            continue
        if raw_request.get("status") not in {
            ExecutionRequestStatus.BLOCKED.value,
            ExecutionRequestStatus.READY.value,
        }:
            continue
        version = raw_request.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            continue
        found[request_id] = (version, raw_request)
    return tuple(
        (request_id, found[request_id][0], found[request_id][1])
        for request_id in sorted(found, key=str)
    )


def _selection_group(
    *,
    group: InputGroupV1,
    evaluation: InputGroupEvaluation,
    selected_keys: tuple[str, ...],
) -> InputSelectionGroupV1:
    priorities = {
        candidate.worker_item_key: candidate.priority for candidate in group.candidates
    }
    ready_sources = {
        source.worker_item_key: source for source in evaluation.ready_sources
    }
    return InputSelectionGroupV1(
        group_key=group.group_key,
        rule_hash=sha256(canonical_json_bytes(group.to_mapping())).hexdigest(),
        sources=tuple(
            _selection_source(
                ready_sources[key],
                priority=priorities[key],
            )
            for key in selected_keys
        ),
    )


def _selection_source(
    source: ResultInputSource,
    *,
    priority: int,
) -> InputSelectionSourceV1:
    if (
        source.worker_item_id is None
        or source.effective_request_id is None
        or not source.allowed_lineage_ids
        or source.effective_execution_id is None
        or source.attempt is None
        or source.source_basis_hash is None
    ):
        raise RuntimeEnvelopeError("ready input source has incomplete provenance")
    return InputSelectionSourceV1(
        worker_item_key=source.worker_item_key,
        worker_item_id=source.worker_item_id,
        subtask_path=source.subtask_path,
        effective_request_id=source.effective_request_id,
        allowed_lineage_ids=source.allowed_lineage_ids,
        effective_lineage_id=source.allowed_lineage_ids[-1],
        effective_execution_id=source.effective_execution_id,
        attempt=source.attempt,
        inheritance_refs=source.inheritance_refs,
        priority=priority,
        source_basis_hash=source.source_basis_hash,
        artifacts=source.artifacts,
        evidence=source.evidence,
    )


def _uses_all_required_policy(
    policy: Mapping[str, object], request: Mapping[str, object]
) -> bool:
    if policy.get("mode") == "disabled":
        return False
    selected: object = policy
    default = policy.get("default_policy")
    if isinstance(default, Mapping):
        selected = default
    overrides = policy.get("subtask_overrides", ())
    if isinstance(overrides, (list, tuple)):
        request_subtask = request.get("subtask_key")
        for item in overrides:
            if isinstance(item, Mapping) and item.get("subtask_key") == request_subtask:
                selected = item
                break
    if not isinstance(selected, Mapping):
        return False
    return (
        selected.get("enabled") is True
        and selected.get("strategy_id") == CollaborationStrategy.ALL_REQUIRED.value
    )


def _released_request_ids(projection: RuntimeProjection) -> set[str]:
    result: set[str] = set()
    for release in projection.releases:
        if (
            release.get("run_id") != str(projection.run_id)
            or release.get("plan_snapshot_id") != str(projection.plan_snapshot_id)
            or release.get("plan_generation") != projection.plan_generation
            or release.get("plan_content_hash") != projection.plan_content_hash
        ):
            continue
        request_id = release.get("execution_request_id")
        if isinstance(request_id, str) and _request_uuid(request_id) is not None:
            result.add(request_id)
    return result


def _request_uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except (TypeError, ValueError):
        return None


def _limit(policy: Mapping[str, object], name: str, default: int) -> int:
    raw_capability = policy.get("runtime_capability_snapshot")
    if not isinstance(raw_capability, Mapping):
        return default
    value = raw_capability.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeEnvelopeError(f"runtime capability {name} is invalid")
    return min(value, default)
