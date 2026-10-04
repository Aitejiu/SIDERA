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
    *,
    human=False,
    confirmed=True,
    capabilities=("acp", "model", "native_tools"),
    auth="unconfirmed",
    requirements=("code", "python"),
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
    worker = replace(
        worker, adapter_type="agent-run", capabilities=frozenset({"code", "python"})
    )
    execution = replace(execution, capability_requirements=frozenset(requirements))
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
        ("missing_mcp_http", "required_capability_unavailable"),
        ("auth_failed", "native_runtime_unavailable"),
    ],
)
def test_native_setup_preserves_requirement_evidence(case, reason):
    uow, worker, execution, services = native_runtime_fixture(
        human=case == "human",
        confirmed=case != "unconfirmed",
        requirements=("code", "python", "mcp_http") if case == "missing_mcp_http" else ("code", "python"),
        auth="failed" if case == "auth_failed" else "unconfirmed",
    )
    (inspection,) = WorkerRuntimeResolver(native_services=services).inspect_candidates(
        uow, (worker,), execution
    )
    assert inspection.resolved is None
    assert inspection.issue.reason_details["reason"] == reason
    assert execution.capability_requirements == frozenset(
        {"code", "python", "mcp_http"}
        if case == "missing_mcp_http"
        else {"code", "python"}
    )
    if case == "human":
        assert services.catalog.calls == []
        assert services.runtimes.calls == []


@pytest.mark.parametrize(
    "status", ("supported", "unsupported", "unconfirmed", "missing")
)
def test_native_scheduler_keeps_explicit_native_capability_gate(status):
    uow, worker, execution, services = native_runtime_fixture(
        requirements=("code", "python", "mcp_http")
    )
    capabilities = dict(services.runtimes.runtime.capabilities)
    if status != "missing":
        capabilities["mcp_http"] = CapabilitySupport(
            status=status, reason_code="controlled", constraints={}
        )
    services.runtimes.runtime = services.runtimes.runtime.model_copy(
        update={"capabilities": capabilities}
    )
    (inspection,) = WorkerRuntimeResolver(native_services=services).inspect_candidates(
        uow, (worker,), execution
    )
    if status == "supported":
        assert inspection.issue is None and inspection.resolved is not None
    else:
        assert inspection.resolved is None
        assert (
            inspection.issue.reason_details["reason"]
            == "required_capability_unavailable"
        )
        assert inspection.issue.reason_details["field"] == "mcp_http"
    assert execution.capability_requirements == frozenset(
        {"code", "python", "mcp_http"}
    )
    assert worker.capabilities == frozenset({"code", "python"})


@pytest.mark.parametrize(
    "case",
    (
        "exact",
        "module",
        "version",
        "missing_plan",
        "generation",
        "root",
        "integration",
        "planning_request",
        "policy",
        "other_policy",
    ),
)
def test_native_setup_slot_eligibility_uses_exact_saved_plan_before_catalog(case):
    from sidera_control_plane.domain.execution_requests import (
        CollaborationGateMode,
        ExecutionRequestStatus,
    )
    from sidera_control_plane.domain.native_setup import NATIVE_SETUP_POLICY_KEY

    uow, worker, execution, services = native_runtime_fixture()
    (configuration,) = uow.worker_runtime_configurations.get_for_workers(
        (worker.id,), execution.workspace_id
    )
    request = SimpleNamespace(
        id=uuid4(),
        workspace_id=execution.workspace_id,
        worker_item_id=execution.worker_item_id,
        status=ExecutionRequestStatus.MATERIALIZED,
        source_execution_id=None,
        source_session_id=None,
        planning_request_id=uuid4(),
        plan_snapshot_id=uuid4(),
        plan_generation=3,
        root_task_id=uuid4(),
        collaboration_gate_mode=CollaborationGateMode.UNMANAGED,
        input_selection_id=None,
        execution_lineage_id=execution.lineage_id,
    )
    planning = SimpleNamespace(
        id=request.planning_request_id,
        planning_profile_id=uuid4(),
        planning_profile_version=2,
    )
    policy = SimpleNamespace(
        profile_key="external" if case == "other_policy" else NATIVE_SETUP_POLICY_KEY,
        execution_mode="native_profile",
    )
    binding = SimpleNamespace(
        workspace_module_ref=uuid4()
        if case in {"module", "other_policy"}
        else configuration.workspace_module_instance_id,
        workspace_config_version=2
        if case == "version"
        else configuration.workspace_module_config_version,
    )
    plan = SimpleNamespace(
        id=request.plan_snapshot_id,
        root_task_id=uuid4() if case == "root" else request.root_task_id,
        generation=4 if case == "generation" else request.plan_generation,
        integration_spec=None
        if case == "integration"
        else SimpleNamespace(verification_binding=binding),
    )
    reads = []
    uow.execution_requests = SimpleNamespace(
        get_effective_by_execution_lineage=lambda *args: request
    )
    uow.planning_requests = SimpleNamespace(
        get=lambda *args: None if case == "planning_request" else planning
    )
    uow.planning_profiles = SimpleNamespace(
        get=lambda policy_id, workspace_id, *, version: (
            reads.append((policy_id, workspace_id, version))
            or (None if case == "policy" else policy)
        )
    )
    uow.plans = SimpleNamespace(
        get=lambda *args: None if case == "missing_plan" else plan
    )
    for locked in (False, True):
        services.catalog.calls.clear()
        result = (
            WorkerRuntimeResolver(native_services=services).lock_and_resolve(
                uow, worker, execution
            )
            if locked
            else WorkerRuntimeResolver(native_services=services).inspect_candidates(
                uow, (worker,), execution
            )[0]
        )
        issue = (
            result
            if locked and hasattr(result, "reason_code")
            else (None if locked else result.issue)
        )
        assert (issue is None) == (case in {"exact", "other_policy"})
        if issue is not None:
            assert services.catalog.calls == [] and services.runtimes.calls == []
    if case != "planning_request":
        assert reads == [(planning.planning_profile_id, execution.workspace_id, 2)] * 2
