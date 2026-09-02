from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.collaboration import CollaborationStrategy
from sidera_control_plane.domain.collaboration_runtime import (
    ReleaseExecutionRequestIntentV1,
    RuntimeDecisionOutcome,
    RuntimeDecisionV1,
    RuntimeEnvelopeError,
    RuntimeProjectionV1,
    canonical_json_bytes,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequestStatus
from sidera_control_plane.domain.planning_profiles import (
    COLLABORATION_MAX_DECISION_BYTES,
    COLLABORATION_MAX_INTENTS_PER_DECISION,
    COLLABORATION_MAX_STRATEGY_STATE_BYTES,
)

Clock = Callable[[], datetime]


class ReferenceCollaborationRuntime:
    """Deterministic reference implementation of the all_required strategy."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate(
        self,
        projection: RuntimeProjectionV1,
        *,
        operation_id: UUID,
        deadline: datetime,
    ) -> RuntimeDecisionV1:
        self._verify_deadline(deadline)
        strategy_state = dict(projection.previous_strategy_state)
        if len(canonical_json_bytes(strategy_state)) > _limit(
            projection.policy,
            "max_strategy_state_bytes",
            COLLABORATION_MAX_STRATEGY_STATE_BYTES,
        ):
            raise RuntimeEnvelopeError("strategy_state exceeds the runtime limit")

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
            reason_code=(
                "eligible_requests" if intents else "no_eligible_requests"
            ),
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
            raise RuntimeEnvelopeError("projection contains duplicate execution request")
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


def _released_request_ids(projection: RuntimeProjectionV1) -> set[str]:
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
