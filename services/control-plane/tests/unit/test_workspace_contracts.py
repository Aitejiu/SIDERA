from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
    WorkspaceLifecycle,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def make_manifest() -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://sidera",
                base_revision="b" * 40,
                branch="sidera/execution/test",
                current_head="b" * 40,
            ),
        ),
        location_ref="git-worktree://execution/test",
        retention_seconds=300,
        fencing_token=1,
        created_at=NOW,
    )


def test_workspace_lifecycle_requires_ordered_transitions() -> None:
    manifest = make_manifest()
    assert manifest.lifecycle is WorkspaceLifecycle.REQUESTED

    manifest.mark_preparing()
    manifest.mark_ready()
    manifest.mark_active()
    manifest.freeze(now=NOW + timedelta(seconds=1))
    manifest.retain(until=NOW + timedelta(minutes=5))
    manifest.begin_cleanup()
    manifest.release(now=NOW + timedelta(minutes=6))

    assert manifest.lifecycle is WorkspaceLifecycle.RELEASED
    assert manifest.frozen_at == NOW + timedelta(seconds=1)


def test_workspace_cannot_be_released_before_freeze() -> None:
    manifest = make_manifest()

    with pytest.raises(InvalidTransition):
        manifest.release(now=NOW)


def test_workspace_manifest_rejects_absolute_location_and_non_positive_retention() -> None:
    with pytest.raises(ValidationError, match="location_ref"):
        ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=uuid4(),
            task_id=uuid4(),
            execution_id=uuid4(),
            checkouts=(),
            location_ref="/tmp/sidera",
            retention_seconds=300,
            fencing_token=1,
            created_at=NOW,
        )

    with pytest.raises(ValidationError, match="retention"):
        ExecutionWorkspaceManifest.create(
            id=uuid4(),
            workspace_id=uuid4(),
            task_id=uuid4(),
            execution_id=uuid4(),
            checkouts=(),
            location_ref="git-worktree://execution/test",
            retention_seconds=0,
            fencing_token=1,
            created_at=NOW,
        )


def test_repository_checkout_requires_absolute_revision_and_writable_scope() -> None:
    with pytest.raises(ValidationError, match="base_revision"):
        RepositoryCheckoutSpec(
            repository_ref="repo://sidera",
            base_revision="main",
            checkout_mode=CheckoutMode.WORKTREE,
            writable_paths=("src",),
            readable_paths=(),
            branch_policy=BranchPolicy.EXECUTION,
        )
    with pytest.raises(ValidationError, match="writable"):
        RepositoryCheckoutSpec(
            repository_ref="repo://sidera",
            base_revision="c" * 40,
            checkout_mode=CheckoutMode.WORKTREE,
            writable_paths=(),
            readable_paths=(),
            branch_policy=BranchPolicy.EXECUTION,
        )
