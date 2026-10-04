from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import (
    ExecutionWorkspace,
    ProfileRef,
    RuntimeRef,
    SourceRef,
)
from sidera_agents.errors import AgentError

from sidera_control_plane.application.execution_authority import (
    ExecutionAuthorityReader,
)
from sidera_control_plane.domain.agent_bindings import ExecutionAuthority
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.persistence.execution_authority_repository import (
    ExecutionAuthorityRepository,
)


def authority():
    workspace = uuid4()
    return ExecutionAuthority(
        id=uuid4(),
        workspace_id=workspace,
        principal_id="operator",
        execution_id=uuid4(),
        target_type="subtask",
        target_id=uuid4(),
        binding_version=1,
        profile_ref=ProfileRef(workspace_id=workspace, profile_id=uuid4(), revision=1),
        runtime_ref=RuntimeRef(workspace_id=workspace, runtime_id=uuid4()),
        lease_id=uuid4(),
        fencing_token=1,
        workspace_ref=uuid4(),
        execution_workspace=ExecutionWorkspace(
            directory="/prepared", writable=True, artifact_directory=None
        ),
        actions=(
            "run.start",
            "workspace.execute",
            "source.read",
            "run.read",
            "run.stop",
        ),
        sources=(SourceRef(provider="control-plane", key="exact", version="v1"),),
    )


@pytest.mark.parametrize("invalid", (True, 1, [], {}, "not-a-grant", "self"))
def test_worker_upstream_provenance_requires_distinct_uuid(invalid):
    value = authority()
    with pytest.raises(ValidationError):
        replace(value, upstream_grant_id=value.id if invalid == "self" else invalid)


@pytest.mark.parametrize(
    "stored", (None, "absent", "valid", True, 1, [], {}, "malformed", "self")
)
def test_worker_policy_decodes_old_missing_and_rejects_malformed_provenance(stored):
    value = authority()
    host = uuid4()
    policy = {
        "principal_id": value.principal_id,
        "profile_ref": value.profile_ref.model_dump(mode="json"),
        "runtime_ref": value.runtime_ref.model_dump(mode="json"),
        "lease_id": str(value.lease_id),
        "fencing_token": value.fencing_token,
        "workspace_ref": str(value.workspace_ref),
        "execution_workspace": value.execution_workspace.model_dump(mode="json"),
        "actions": list(value.actions),
        "sources": [ref.model_dump(mode="json") for ref in value.sources],
        "credentials": [],
        "tools": [],
        "resources": [],
    }
    if stored != "absent":
        policy["upstream_grant_id"] = (
            str(host)
            if stored == "valid"
            else str(value.id)
            if stored == "self"
            else stored
        )
    row = SimpleNamespace(
        **{
            name: getattr(value, name)
            for name in (
                "id",
                "workspace_id",
                "execution_id",
                "target_type",
                "target_id",
                "binding_version",
                "revoked",
            )
        },
        policy=policy,
    )
    repository = ExecutionAuthorityRepository(SimpleNamespace(scalar=lambda _: row))
    if stored is None or stored == "absent" or stored == "valid":
        loaded = repository.get(value.id)
        assert loaded.upstream_grant_id == (host if stored == "valid" else None)
    else:
        with pytest.raises((ValidationError, ValueError, TypeError)):
            repository.get(value.id)


@pytest.mark.parametrize("old", (False, True))
def test_cp_reader_uses_saved_host_grant_and_preserves_old_original_cleanup(old):
    now, host = datetime.now(UTC), uuid4()
    value = replace(authority(), upstream_grant_id=None if old else host)
    binding = SimpleNamespace(version=1, profile_ref=value.profile_ref, runnable=True)
    link = SimpleNamespace(
        authority_id=value.id,
        binding_version=1,
        profile_ref=value.profile_ref,
        status="pending",
        run_id=None,
    )
    execution = SimpleNamespace(
        worker_item_id=None,
        task_id=value.target_id,
        stop_requested=False,
        handoff_transition_id=None,
        status=SimpleNamespace(value="leased"),
        active_lease_id=value.lease_id,
        last_fencing_token=1,
    )
    lease = SimpleNamespace(
        id=value.lease_id,
        execution_id=value.execution_id,
        status=SimpleNamespace(value="active"),
        expires_at=now + timedelta(minutes=5),
        fencing_token=1,
    )
    manifest = SimpleNamespace(
        execution_id=value.execution_id,
        fencing_token=1,
        lifecycle=SimpleNamespace(value="ready"),
        location_ref="prepared://exact",
    )
    uow = SimpleNamespace(
        execution_authorities=SimpleNamespace(
            get=lambda _: value,
            get_link=lambda *args: link,
            prepared_location_ref=lambda *args: manifest.location_ref,
        ),
        executions=SimpleNamespace(get=lambda *args: execution),
        leases=SimpleNamespace(get=lambda *args: lease),
        agent_bindings=SimpleNamespace(
            history=lambda *args: (binding,), get=lambda *args: binding
        ),
        workspaces=SimpleNamespace(get=lambda *args: manifest),
    )
    calls = []

    def require(grant, action, target):
        calls.append((grant, action, target))
        assert (
            grant == host
            and target.workspace_id == value.workspace_id
            and target.target_id == value.target_id
        )

    reader = ExecutionAuthorityReader(
        lambda: nullcontext(uow),
        principal=lambda: value.principal_id,
        authorizer=SimpleNamespace(require=require),
        clock=lambda: now,
    )
    reader.require(value.id, "run.read", value.workspace_id)
    reader.require(value.id, "run.stop", value.workspace_id)
    assert not calls
    for action, target in (
        ("run.start", value.profile_ref),
        ("workspace.execute", value.execution_workspace),
        ("source.read", value.sources[0]),
    ):
        if old:
            with pytest.raises(AgentError):
                reader.require(value.id, action, target)
        else:
            reader.require(value.id, action, target)
    assert [grant for grant, _, _ in calls] == ([] if old else [host] * 3)
