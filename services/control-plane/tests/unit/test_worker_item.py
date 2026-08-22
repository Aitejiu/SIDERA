from datetime import UTC, datetime
from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.worker_item import WorkerItem, WorkerItemStatus

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("73000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 22, tzinfo=UTC)


def make_worker_item() -> WorkerItem:
    return WorkerItem.create(
        item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        title="Implement callback retries",
        responsibility="Keep callback retry behavior idempotent.",
        now=NOW,
    )


def test_worker_item_starts_with_acknowledged_context() -> None:
    item = make_worker_item()

    assert item.status is WorkerItemStatus.ACTIVE
    assert item.stable_key == f"worker-item:{WORKER_ITEM_ID}"
    assert item.context_version == 1
    assert item.acknowledged_context_version == 1
    assert item.context_is_synchronized
    assert item.version == 1
    assert item.created_at == NOW
    assert item.updated_at == NOW


def test_context_patch_closes_writes_until_acknowledged() -> None:
    item = make_worker_item()

    item.advance_context(now=NOW)
    assert (item.context_version, item.acknowledged_context_version) == (2, 1)
    assert not item.context_is_synchronized
    assert item.version == 2

    item.acknowledge_context(expected_context_version=2, now=NOW)
    assert item.context_is_synchronized
    assert item.version == 3


def test_context_acknowledgement_rejects_stale_expected_version() -> None:
    item = make_worker_item()
    item.advance_context(now=NOW)

    with pytest.raises(ValidationError, match="context version"):
        item.acknowledge_context(expected_context_version=1, now=NOW)


def test_pause_and_resume_preserve_worker_item_identity() -> None:
    item = make_worker_item()

    item.pause(now=NOW)
    assert item.status is WorkerItemStatus.PAUSED
    item.resume(now=NOW)
    assert item.status is WorkerItemStatus.ACTIVE
    assert item.id == WORKER_ITEM_ID
    assert item.version == 3


def test_retired_worker_item_cannot_resume_or_accept_context_changes() -> None:
    item = make_worker_item()
    item.retire(now=NOW)

    with pytest.raises(InvalidTransition):
        item.resume(now=NOW)
    with pytest.raises(InvalidTransition):
        item.advance_context(now=NOW)
    with pytest.raises(InvalidTransition):
        item.acknowledge_context(expected_context_version=1, now=NOW)


def test_worker_item_rejects_empty_identity_and_duplicate_lineage() -> None:
    with pytest.raises(ValidationError, match="title and responsibility"):
        WorkerItem.create(
            item_id=WORKER_ITEM_ID,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            title=" ",
            responsibility=" ",
            now=NOW,
        )

    with pytest.raises(ValidationError, match="derived_from"):
        WorkerItem.create(
            item_id=WORKER_ITEM_ID,
            workspace_id=WORKSPACE_ID,
            root_task_id=ROOT_TASK_ID,
            title="Implement callback retries",
            responsibility="Keep callback retry behavior idempotent.",
            derived_from_worker_item_ids=(WORKER_ITEM_ID, WORKER_ITEM_ID),
            now=NOW,
        )
