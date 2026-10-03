from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sidera_agents.contracts import (
    CapabilitySupport,
    ProfileDraft,
    ProfileRef,
    ProfileRevision,
    RuntimeRef,
    RuntimeSnapshot,
)

from sidera_control_plane.application.agent_worker_adapter import (
    NativeWorkerServices,
    native_capabilities,
)
from sidera_control_plane.application.worker_runtime import WorkerRuntimeResolver
from sidera_control_plane.domain.agent_bindings import AgentBinding, AssigneeRef
from tests.support.native_agents import PublicAgentCatalog, PublicAgentRuntimes
from tests.unit.test_worker_runtime_resolver import (
    NOW,
    WORKSPACE_ID,
    make_fixture,
    make_uow,
)


def native_runtime_fixture(
    *, human=False, confirmed=True, capabilities=("code",), auth="unconfirmed"
):
    (
        worker,
        execution,
        configuration,
        adapter,
        workspace,
        adapter_definition,
        workspace_definition,
    ) = make_fixture(adapter_provider="agent-run")
    worker = replace(worker, adapter_type="agent-run", capabilities=frozenset({"code"}))
    execution = replace(execution, capability_requirements=frozenset({"code"}))
    uow = make_uow(
        (configuration,),
        (adapter, workspace),
        (adapter_definition, workspace_definition),
    )
    runtime_ref = RuntimeRef(workspace_id=WORKSPACE_ID, runtime_id=uuid4())
    profile = ProfileRevision(
        ref=ProfileRef(workspace_id=WORKSPACE_ID, profile_id=uuid4(), revision=1),
        draft=ProfileDraft(
            name="Unit",
            description="",
            runtime_ref=runtime_ref,
            instructions="",
            resource_refs=(),
            model_id=None,
            thinking=None,
            max_active=2,
            environment={},
            secret_refs=(),
            native_settings={},
        ),
        config_hash="unit",
        created_at=NOW,
    )
    binding = AgentBinding(
        WORKSPACE_ID,
        "subtask",
        execution.task_id,
        AssigneeRef("human", "operator")
        if human
        else AssigneeRef("profile", profile.ref.profile_id),
        None if human else profile.ref,
        1,
        NOW,
        NOW,
        not human,
    )
    uow.agent_bindings = SimpleNamespace(
        get=lambda *args: binding if confirmed else None
    )
    catalog = PublicAgentCatalog((profile,))
    runtimes = PublicAgentRuntimes(
        RuntimeSnapshot(
            ref=runtime_ref,
            harness_id="fixture",
            executable="/unused",
            actual_version="1",
            installed=True,
            enabled=True,
            version=1,
            protocol_state="ready",
            auth_state=auth,
            capabilities={
                key: CapabilitySupport(
                    status="supported", reason_code="fixture_only", constraints={}
                )
                for key in capabilities
            },
            checked_at=NOW,
            limits={},
        )
    )
    services = NativeWorkerServices(catalog, runtimes, None, None, "operator", uuid4())
    return uow, worker, execution, services


def test_unconfirmed_auth_alone_does_not_block_a_pinned_profile():
    uow, worker, execution, services = native_runtime_fixture()
    (inspection,) = WorkerRuntimeResolver(native_services=services).inspect_candidates(
        uow, (worker,), execution
    )
    assert inspection.issue is None
    assert inspection.resolved.capability_snapshot == native_capabilities()
    assert inspection.resolved.resolution_facts["execution_mode"] == "native"
    assert inspection.resolved.resolution_facts["profile_ref"] == next(
        iter(services.catalog.profiles)
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "case,reason",
    [
        ("human", "human_manual"),
        ("unconfirmed", "binding_confirmation_required"),
        ("missing_code", "required_capability_unavailable"),
        ("auth_failed", "native_runtime_unavailable"),
    ],
)
def test_native_setup_preserves_requirement_evidence(case, reason):
    uow, worker, execution, services = native_runtime_fixture(
        human=case == "human",
        confirmed=case != "unconfirmed",
        capabilities=("native_tools", "mcp") if case == "missing_code" else ("code",),
        auth="failed" if case == "auth_failed" else "unconfirmed",
    )
    (inspection,) = WorkerRuntimeResolver(native_services=services).inspect_candidates(
        uow, (worker,), execution
    )
    assert inspection.resolved is None
    assert inspection.issue.reason_details["reason"] == reason
    assert execution.capability_requirements == frozenset({"code"})
    if case == "human":
        assert services.catalog.calls == []
        assert services.runtimes.calls == []
