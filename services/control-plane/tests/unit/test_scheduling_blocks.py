from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.scheduling import (
    SchedulingBlock,
    SchedulingBlockReason,
    SchedulingBlockStatus,
    SchedulingIssue,
    scheduling_fingerprint,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
EXECUTION_ID = UUID("24000000-0000-0000-0000-000000000001")
BLOCK_ID = UUID("25000000-0000-0000-0000-000000000001")


def issue(
    *,
    reason: SchedulingBlockReason = SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING,
    details: dict[str, object] | None = None,
    requires_operator: bool = True,
) -> SchedulingIssue:
    return SchedulingIssue(
        reason_code=reason,
        reason_details=details or {"missing_count": 1},
        requires_operator=requires_operator,
    )


def test_fingerprint_is_stable_for_mapping_order() -> None:
    first = scheduling_fingerprint(
        SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
        {"candidate_count": 2, "version": 4},
        requires_operator=True,
    )
    second = scheduling_fingerprint(
        SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
        {"version": 4, "candidate_count": 2},
        requires_operator=True,
    )

    assert first == second
    assert first != scheduling_fingerprint(
        SchedulingBlockReason.RUNTIME_CONFIGURATION_INVALID,
        {"candidate_count": 3, "version": 4},
        requires_operator=True,
    )


def test_new_block_starts_as_one_occurrence_active_episode() -> None:
    block = SchedulingBlock.open(
        block_id=BLOCK_ID,
        workspace_id=WORKSPACE_ID,
        execution_id=EXECUTION_ID,
        issue=issue(),
        now=NOW,
    )

    assert block.status is SchedulingBlockStatus.ACTIVE
    assert block.reason_code is SchedulingBlockReason.RUNTIME_CONFIGURATION_MISSING
    assert block.first_seen_at == NOW
    assert block.last_seen_at == NOW
    assert block.occurrence_count == 1
    assert block.version == 1
    assert block.resolved_at is None
    with pytest.raises(TypeError):
        block.reason_details["new"] = "value"  # type: ignore[index]


def test_observe_same_fingerprint_updates_count_without_changing_episode() -> None:
    block = SchedulingBlock.open(
        block_id=BLOCK_ID,
        workspace_id=WORKSPACE_ID,
        execution_id=EXECUTION_ID,
        issue=issue(),
        now=NOW,
    )
    observed_at = NOW + timedelta(seconds=5)

    block.observe(issue=issue(), now=observed_at)

    assert block.id == BLOCK_ID
    assert block.status is SchedulingBlockStatus.ACTIVE
    assert block.first_seen_at == NOW
    assert block.last_seen_at == observed_at
    assert block.occurrence_count == 2
    assert block.version == 2


def test_observe_rejects_a_changed_reason_inside_the_same_episode() -> None:
    block = SchedulingBlock.open(
        block_id=BLOCK_ID,
        workspace_id=WORKSPACE_ID,
        execution_id=EXECUTION_ID,
        issue=issue(),
        now=NOW,
    )

    with pytest.raises(ValidationError, match="fingerprint"):
        block.observe(
            issue=issue(
                reason=SchedulingBlockReason.ADAPTER_MODULE_UNAVAILABLE,
            ),
            now=NOW + timedelta(seconds=1),
        )


def test_resolve_closes_active_episode_once() -> None:
    block = SchedulingBlock.open(
        block_id=BLOCK_ID,
        workspace_id=WORKSPACE_ID,
        execution_id=EXECUTION_ID,
        issue=issue(),
        now=NOW,
    )
    resolved_at = NOW + timedelta(minutes=1)

    block.resolve(now=resolved_at)

    assert block.status is SchedulingBlockStatus.RESOLVED
    assert block.resolved_at == resolved_at
    assert block.version == 2
    with pytest.raises(InvalidTransition, match="resolved"):
        block.resolve(now=resolved_at + timedelta(seconds=1))


def test_scheduling_issue_rejects_non_json_details() -> None:
    with pytest.raises(ValidationError, match="JSON"):
        SchedulingIssue(
            reason_code=SchedulingBlockReason.NO_ELIGIBLE_WORKER,
            reason_details={"worker": object()},
            requires_operator=False,
        )
