from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from sidera_agents.catalog import ProfileCatalog
from sidera_agents.contracts import (
    AuthenticationState,
    ProfileDraft,
)
from sidera_agents.errors import AgentError
from sidera_agents.harnesses import opencode_descriptor
from sidera_agents.postgres.models import RuntimeInstallationRow
from sidera_agents.postgres.store import PostgresAgentStore
from sidera_agents.runtimes import RuntimeCatalog


class _WorkspaceAuthorizer:
    def __init__(self, grants):
        self._grants = grants

    def require(self, grant_id, action, target):
        workspace_id = getattr(target, "workspace_id", target)
        if self._grants.get(grant_id) != workspace_id:
            raise AgentError("permission_denied", "Access denied.")


def test_runtime_references_and_shared_installation_persist(
    owned_agents_pg, monkeypatch, tmp_path
):
    from sqlalchemy import func, select

    engine = owned_agents_pg.engine
    owned_agents_pg.migrate()
    monkeypatch.setattr(opencode_descriptor, "read_version", lambda _: "1.4.2")
    executable = tmp_path / "opencode"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    workspace_a, workspace_b = uuid4(), uuid4()
    grant_a, grant_b = uuid4(), uuid4()
    authorizer = _WorkspaceAuthorizer({grant_a: workspace_a, grant_b: workspace_b})
    clock = lambda: datetime(2026, 10, 1, tzinfo=UTC)
    store_a = PostgresAgentStore(engine)
    runtimes_a = RuntimeCatalog(store_a, authorizer, clock=clock)
    runtime_a = runtimes_a.register(
        workspace_a,
        "opencode",
        str(executable),
        max_active=2,
        max_sessions=4,
        grant_id=grant_a,
    )
    profile_catalog = ProfileCatalog(store_a, authorizer, clock)
    profile = profile_catalog.create_profile(
        workspace_a,
        ProfileDraft(
            name="reviewer",
            description="Reviews changes",
            runtime_ref=runtime_a.ref,
            instructions="Review the change carefully.",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=1,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        grant_id=grant_a,
    )

    reopened_store = PostgresAgentStore(engine)
    reopened_runtimes = RuntimeCatalog(reopened_store, authorizer, clock=clock)
    first_validation = reopened_runtimes.validate_profile(profile.ref, grant_id=grant_a)
    assert first_validation.valid is True
    assert {issue.code for issue in first_validation.issues} == {
        "runtime_protocol_unconfirmed",
        "authentication_unconfirmed",
    }

    saved_entry = reopened_store.get_runtime_entry(runtime_a.ref)
    failed_observation = replace(
        saved_entry.record.observation,
        auth_state=AuthenticationState.FAILED,
    )
    reopened_store.update_runtime_observation(runtime_a.ref, failed_observation)
    validation_after_restart = RuntimeCatalog(
        PostgresAgentStore(engine), authorizer, clock=clock
    ).validate_profile(profile.ref, grant_id=grant_a)
    assert validation_after_restart.valid is False
    assert any(
        issue.code == "authentication_failed"
        for issue in validation_after_restart.issues
    )

    runtime_b = RuntimeCatalog(
        PostgresAgentStore(engine), authorizer, clock=clock
    ).register(
        workspace_b,
        "opencode",
        str(executable),
        max_active=20,
        max_sessions=40,
        grant_id=grant_b,
    )
    entry_a = PostgresAgentStore(engine).get_runtime_entry(runtime_a.ref)
    entry_b = PostgresAgentStore(engine).get_runtime_entry(runtime_b.ref)
    with engine.connect() as connection:
        installation_count = connection.scalar(
            select(func.count()).select_from(RuntimeInstallationRow)
        )

    assert runtime_a.ref != runtime_b.ref
    assert entry_a.record.installation_key == entry_b.record.installation_key
    assert entry_b.installation.max_active == 2
    assert entry_b.installation.max_sessions == 4
    assert entry_a.installation.active_count == entry_b.installation.active_count == 0
    assert entry_a.installation.session_count == entry_b.installation.session_count == 0
    assert installation_count == 1
