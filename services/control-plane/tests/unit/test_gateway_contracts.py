from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sidera_control_plane.domain.artifacts import EvidenceClaim, EvidenceClaimStatus
from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
)
from sidera_control_plane.domain.modules import (
    CapabilityInput,
    CapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    ResumeCapability,
    SandboxCapability,
    StabilityLevel,
    StopCapability,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def make_capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        input=CapabilityInput.QUEUED,
        idempotency=IdempotencyCapability.NONE,
        resume=ResumeCapability.SAME_PROCESS,
        stop=StopCapability.EXACT_EXECUTION,
        sandbox=SandboxCapability.FILESYSTEM,
        stability=StabilityLevel.EXPERIMENTAL,
    )


def make_checkout() -> RepositoryCheckoutSpec:
    return RepositoryCheckoutSpec(
        repository_ref="repo://sidera",
        base_revision="a" * 40,
        checkout_mode=CheckoutMode.WORKTREE,
        writable_paths=("src",),
        readable_paths=("tests",),
        branch_policy=BranchPolicy.EXECUTION,
    )


def make_execution_spec(repository: tuple[RepositoryCheckoutSpec, ...]):
    workspace_id = uuid4()
    return ExecutionSpec(
        schema_version=1,
        workspace_id=workspace_id,
        task_id=uuid4(),
        execution_id=uuid4(),
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Implement the gateway contract",
        acceptance_criteria=("contract tests pass",),
        context_package_ref=None,
        repositories=repository,
        tool_policy={"shell": "allowed"},
        sandbox_policy={"mode": "filesystem"},
        budget={"max_seconds": 60},
        deadline=NOW,
        expected_outputs=("diff",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
    )


def test_capability_profile_rejects_unknown_advertised_level() -> None:
    with pytest.raises(ValidationError, match="input capability"):
        CapabilityProfile(
            input="live" ,  # type: ignore[arg-type]
            idempotency=IdempotencyCapability.NONE,
            resume=ResumeCapability.NONE,
            stop=StopCapability.PROCESS,
            sandbox=SandboxCapability.NONE,
            stability=StabilityLevel.EXPERIMENTAL,
        )


def test_module_instance_requires_namespaced_config() -> None:
    definition = ModuleDefinition(
        id=uuid4(),
        workspace_id=uuid4(),
        module_type="worker_adapter",
        provider="fake",
        interface_version="v1",
        config_schema_version=1,
        capabilities=make_capabilities(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )

    with pytest.raises(ValidationError, match="namespace"):
        ModuleInstance(
            id=uuid4(),
            workspace_id=definition.workspace_id,
            definition_id=definition.id,
            instance_name="fake-default",
            config_version=1,
            config_namespace="",
            config={"command": "fake"},
            secret_refs=(),
            lifecycle=ModuleLifecycle.ACTIVE,
        )


def test_execution_spec_requires_exactly_one_repository() -> None:
    with pytest.raises(ValidationError, match="exactly one repository"):
        make_execution_spec(())
    with pytest.raises(ValidationError, match="exactly one repository"):
        make_execution_spec((make_checkout(), make_checkout()))


def test_gateway_operation_unknown_result_is_terminal_until_explicit_retry() -> None:
    operation_id = uuid4()
    operation = GatewayOperation.create(
        id=uuid4(),
        operation_id=operation_id,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        session_id=None,
        operation_type=GatewayOperationType.START_SESSION,
        module_instance_id=uuid4(),
        config_version=1,
        lease_id=uuid4(),
        fencing_token=1,
        now=NOW,
    )
    operation.start(now=NOW)
    operation.mark_unknown(failure_code="response_lost", now=NOW)

    with pytest.raises(InvalidTransition):
        operation.succeed(result_ref="result://late", now=NOW)

    operation.retry(now=NOW)
    assert operation.status is GatewayOperationStatus.REQUESTED
    assert operation.operation_id == operation_id
    assert operation.attempt_count == 2


def test_evidence_claim_is_candidate_only() -> None:
    claim = EvidenceClaim.create_candidate(
        id=uuid4(),
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=uuid4(),
        acceptance_criterion_ref="tests.pass",
        conclusion="The contract tests passed",
        environment_ref="env://ci",
        artifact_ids=(uuid4(),),
        producer="gateway",
        created_at=NOW,
    )

    assert claim.status is EvidenceClaimStatus.CANDIDATE
    assert not hasattr(claim, "accept")
