from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    RuntimeRef,
)
from sidera_agents.errors import AgentError


class OwnedWorkspaceAuthorizer:
    def __init__(self, workspace_id: UUID, grant_id: UUID) -> None:
        self.workspace_id = workspace_id
        self.grant_id = grant_id
        self.calls: list[tuple[UUID, str, object]] = []

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        self.calls.append((grant_id, action, target))
        target_workspace = getattr(target, "workspace_id", target)
        if grant_id != self.grant_id or target_workspace != self.workspace_id:
            raise AgentError("permission_denied", "Access denied.")


@pytest.fixture
def catalog_lab():
    from sidera_agents.catalog import ProfileCatalog
    from sidera_agents.memory import MemoryAgentStore

    workspace_id = uuid4()
    grant_id = uuid4()
    runtime_ref = RuntimeRef(workspace_id=workspace_id, runtime_id=uuid4())
    skill_draft = CapabilityResourceDraft(
        name="review",
        kind="skill",
        settings={"body": "Check tests"},
        secret_refs=(),
    )
    draft = ProfileDraft(
        name="reviewer",
        description="Reviews changes",
        runtime_ref=runtime_ref,
        instructions="Review carefully.",
        resource_refs=(),
        model_id=None,
        thinking=None,
        max_active=1,
        environment={},
        secret_refs=(),
        native_settings={},
    )
    authorizer = OwnedWorkspaceAuthorizer(workspace_id, grant_id)
    clock = lambda: datetime(2026, 10, 1, tzinfo=UTC)
    catalog = ProfileCatalog(
        MemoryAgentStore(),
        authorizer,
        clock,
    )
    return type(
        "CatalogLab",
        (),
        {
            "catalog": catalog,
            "workspace_id": workspace_id,
            "runtime_ref": runtime_ref,
            "draft": draft,
            "grant_id": grant_id,
            "skill_draft": skill_draft,
            "authorizer": authorizer,
            "clock": clock,
        },
    )()
