from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.task import Task, TaskStatus


def make_task() -> Task:
    return Task.create(
        task_id=uuid4(),
        workspace_id=uuid4(),
        title="Build task API",
        description="Create the first deterministic slice",
        acceptance_criteria=("Duplicate commands are idempotent",),
        priority=50,
        requested_by="user:test",
    )


def test_create_task_starts_pending_at_version_one():
    task = make_task()

    assert task.status is TaskStatus.PENDING
    assert task.version == 1


def test_title_and_acceptance_criteria_are_required():
    with pytest.raises(ValidationError):
        Task.create(
            task_id=uuid4(),
            workspace_id=uuid4(),
            title=" ",
            description="",
            acceptance_criteria=(),
            priority=50,
            requested_by="user:test",
        )


@pytest.mark.parametrize("priority", [-1, 101])
def test_priority_must_be_between_zero_and_one_hundred(priority: int):
    with pytest.raises(ValidationError, match="priority"):
        Task.create(
            task_id=uuid4(),
            workspace_id=uuid4(),
            title="Build task API",
            description="",
            acceptance_criteria=("Priority is bounded",),
            priority=priority,
            requested_by="user:test",
        )


def test_pause_resume_cancel_increment_version():
    task = make_task()

    task.pause()
    assert (task.status, task.version) == (TaskStatus.PAUSED, 2)

    task.resume()
    assert (task.status, task.version) == (TaskStatus.PENDING, 3)

    task.cancel()
    assert (task.status, task.version) == (TaskStatus.CANCELED, 4)


def test_canceled_task_rejects_mutation():
    task = make_task()
    task.cancel()

    with pytest.raises(InvalidTransition):
        task.set_priority(80)


def test_only_paused_task_can_resume():
    task = make_task()

    with pytest.raises(InvalidTransition, match="paused"):
        task.resume()


def test_paused_task_cannot_be_paused_again():
    task = make_task()
    task.pause()

    with pytest.raises(InvalidTransition, match="already paused"):
        task.pause()


def test_task_readiness_lifecycle_supports_waiting_and_retry():
    task = make_task()

    task.mark_ready()
    assert task.status is TaskStatus.READY
    assert task.version == 2

    task.mark_running()
    assert task.status is TaskStatus.RUNNING
    assert task.version == 3

    task.wait_for_decision()
    assert task.status is TaskStatus.WAITING
    assert task.version == 4

    task.mark_ready()
    assert task.status is TaskStatus.READY
    assert task.version == 5


def test_task_readiness_transitions_reject_invalid_states():
    task = make_task()
    with pytest.raises(InvalidTransition):
        task.wait_for_decision()

    task.cancel()
    with pytest.raises(InvalidTransition):
        task.mark_ready()
