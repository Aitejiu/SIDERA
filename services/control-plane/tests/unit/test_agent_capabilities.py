from importlib import import_module

import pytest

from sidera_control_plane.application.assignments import (
    AssignmentCommandHandler,
    TrustedAssignmentIdentity,
)
from tests.unit.test_native_profile_scheduler import native_runtime_fixture


@pytest.mark.parametrize(
    "requirements,expected",
    [
        (["code", "python"], ()),
        (["python", "mcp_http", "code"], ("mcp_http",)),
        (["unknown_feature", "code"], ("unknown_feature",)),
        (["skill", "mcp", "skill", "python", "acp"], ("acp", "mcp", "skill")),
        ([], ()),
    ],
)
def test_native_requirements_project_only_known_cp_labels_without_mutating_input(
    requirements, expected
):
    projection = import_module(
        "sidera_control_plane.application.agent_capabilities"
    ).runtime_capability_requirements
    original = list(requirements)
    assert projection(requirements) == expected
    assert requirements == original


@pytest.mark.parametrize(
    "status", ("supported", "unsupported", "unconfirmed", "missing")
)
def test_exact_profile_resolution_preserves_genuine_native_requirements(status):
    from sidera_agents.contracts import CapabilitySupport

    _, _, execution, services = native_runtime_fixture()
    runtime = services.runtimes.runtime
    capabilities = dict(runtime.capabilities)
    if status != "missing":
        capabilities["mcp_http"] = CapabilitySupport(
            status=status, reason_code="mcp_http_" + status, constraints={}
        )
    services.runtimes.runtime = runtime.model_copy(
        update={"capabilities": capabilities}
    )
    profile_ref = next(iter(services.catalog.profiles))
    handler = AssignmentCommandHandler(
        catalog=services.catalog,
        runtimes=services.runtimes,
        identity=TrustedAssignmentIdentity(
            services.principal_id, services.catalog_grant_id
        ),
    )
    ref, reasons = handler.resolve_profile(
        execution.workspace_id,
        profile_ref.profile_id,
        profile_ref.revision,
        frozenset({"code", "python", "mcp_http"}),
    )
    assert ref == profile_ref
    assert reasons == (
        ()
        if status == "supported"
        else (
            "required_capability_unavailable:mcp_http"
            if status == "missing"
            else "mcp_http_" + status,
        )
    )
    assert "code" not in capabilities and "python" not in capabilities
