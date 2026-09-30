from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.local_verification import (
    LocalVerificationAdapter,
    LocalVerificationAdapterError,
)
from sidera_control_plane.application.result_ports import (
    CheckRunObservation,
    CheckRunRequest,
    CheckRunStatus,
)
from sidera_control_plane.domain.context_packages import (
    ContextMaterializationV1,
    ContextMaterializationV3,
    context_package_reference,
)
from sidera_control_plane.domain.gateway import DeliveryOutcome
from sidera_control_plane.domain.result_integration import CheckSpecV1
from sidera_control_plane.domain.result_verification import (
    EnvironmentBindingV1,
    VerificationCheckRefV1,
    VerificationManifestV1,
)
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    ExecutionWorkspaceManifest,
    RepositoryCheckoutSpec,
    WorkspaceCheckout,
)
from tests.unit.test_context_package_v2 import _materialization_v1

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
COMMIT = "c" * 40
TREE = "d" * 40
CANDIDATE_ID = UUID("90000000-0000-0000-0000-000000000001")
CANDIDATE_HASH = "a4a7b7a5c37e1c6231fb37dc17cafcdb8ff6ad5394e2925613e54447baf73cf4"


def _canonical_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _check_spec() -> CheckSpecV1:
    return CheckSpecV1(
        key="unit",
        version=1,
        argv=("python3", "-m", "unittest"),
        cwd=".",
        environment_ref="env://python-test",
        prepare_argvs=(),
        timeout_seconds=60,
        success_exit_codes=(0,),
        report_paths=(),
        required=True,
    )


def _check_hash(check: CheckSpecV1) -> str:
    return _canonical_hash(check.to_mapping())


def _verification_manifest(check: CheckSpecV1) -> VerificationManifestV1:
    check_ref = VerificationCheckRefV1(
        key=check.key,
        version=check.version,
        spec_hash=_check_hash(check),
    )
    basis = {
        "schema_version": 1,
        "candidate_id": str(CANDIDATE_ID),
        "candidate_hash": CANDIDATE_HASH,
        "result_commit": COMMIT,
        "result_tree": TREE,
        "checks": [check_ref.to_mapping()],
        "environment_binding": EnvironmentBindingV1(
            ref=check.environment_ref,
            version=1,
        ).to_mapping(),
    }
    return VerificationManifestV1(
        schema_version=1,
        candidate_id=CANDIDATE_ID,
        candidate_hash=CANDIDATE_HASH,
        result_commit=COMMIT,
        result_tree=TREE,
        checks=(check_ref,),
        environment_binding=EnvironmentBindingV1(
            ref=check.environment_ref,
            version=1,
        ),
        verification_basis_hash=_canonical_hash(basis),
    )


def _materialization(execution_id: UUID, *, verification: bool = True) -> ContextMaterializationV3:
    legacy = _materialization_v1()
    manifest = _verification_manifest(_check_spec()) if verification else None
    return ContextMaterializationV3(
        metadata=replace(legacy.metadata, consumer_id=execution_id),
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        result_inputs=(),
        code_input_manifest=None,
        verification_manifest=manifest,
    )


def _spec(
    *,
    materialization: ContextMaterializationV3 | ContextMaterializationV1,
    purpose: str = "verification",
) -> ExecutionSpec:
    execution_id = materialization.metadata.consumer_id
    verification_manifest = materialization.verification_manifest
    assert verification_manifest is not None
    return ExecutionSpec(
        schema_version=3,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=execution_id,
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Verify the published integration candidate",
        acceptance_criteria=("the configured check passes",),
        context_package_ref=context_package_reference(
            materialization.metadata.package_id,
            materialization.metadata.package_hash,
        ),
        repositories=(
            RepositoryCheckoutSpec(
                repository_ref="repo://integration-test",
                base_revision="a" * 40,
                checkout_mode=CheckoutMode.WORKTREE,
                writable_paths=(".",),
                readable_paths=(".",),
                branch_policy=BranchPolicy.EXECUTION,
            ),
        ),
        tool_policy={},
        sandbox_policy={"mode": "filesystem"},
        budget={},
        deadline=NOW + timedelta(minutes=1),
        expected_outputs=("verification result",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        context_package=materialization,
        execution_purpose=purpose,  # type: ignore[arg-type]
        physical_input_ack={
            "status": "verification_input_bound",
            "execution_id": str(execution_id),
            "candidate_id": str(CANDIDATE_ID),
            "candidate_hash": CANDIDATE_HASH,
            "actual_commit": COMMIT,
            "actual_tree": TREE,
            "verification_basis_hash": verification_manifest.verification_basis_hash,
        },
    )


def _workspace(spec: ExecutionSpec, location: Path) -> ExecutionWorkspaceManifest:
    return ExecutionWorkspaceManifest.create(
        id=uuid4(),
        workspace_id=spec.workspace_id,
        task_id=spec.task_id,
        execution_id=spec.execution_id,
        checkouts=(
            WorkspaceCheckout(
                repository_ref="repo://integration-test",
                base_revision="a" * 40,
                branch=f"sidera/execution/{spec.execution_id}",
                current_head="a" * 40,
            ),
        ),
        location_ref=f"git-worktree://{spec.execution_id}",
        retention_seconds=3600,
        fencing_token=spec.fencing_token,
        created_at=NOW,
    )


@dataclass
class CapturingRunner:
    observations: dict[UUID, CheckRunObservation]
    starts: list[CheckRunRequest]
    stops: list[UUID]

    def __init__(self) -> None:
        self.observations = {}
        self.starts = []
        self.stops = []

    def start(self, request: CheckRunRequest) -> CheckRunObservation:
        self.starts.append(request)
        observation = CheckRunObservation(
            operation_id=request.operation_id,
            status=CheckRunStatus.RUNNING,
            pid=None,
            process_start_identity=None,
            started_at=NOW,
            finished_at=None,
            exit_code=None,
            log_refs=(f"{request.log_ref}.stdout.log", f"{request.log_ref}.stderr.log"),
            actual_commit=request.actual_commit,
            actual_tree=request.actual_tree,
            source_dirty=False,
            report_availability=(),
            failure_code=None,
        )
        self.observations[request.operation_id] = observation
        return observation

    def inspect(self, operation_id: UUID) -> CheckRunObservation | None:
        return self.observations.get(operation_id)

    def stop(self, operation_id: UUID) -> CheckRunObservation | None:
        self.stops.append(operation_id)
        return self.observations.get(operation_id)

    def finish(self, operation_id: UUID, status: CheckRunStatus) -> None:
        current = self.observations[operation_id]
        self.observations[operation_id] = replace(
            current,
            status=status,
            finished_at=NOW + timedelta(seconds=1),
            exit_code=0 if status is CheckRunStatus.PASSED else 1,
        )


@pytest.fixture
def verifier_case(tmp_path: Path) -> dict[str, Any]:
    runner = CapturingRunner()
    adapter = LocalVerificationAdapter(
        runner=runner,
        check_specs={"unit": _check_spec()},
        workspace_locator=lambda manifest: tmp_path,
    )
    execution_id = uuid4()
    materialization = _materialization(execution_id)
    spec = _spec(materialization=materialization)
    workspace = _workspace(spec, tmp_path)
    operation_id = uuid4()
    handle = adapter.start_session(spec, workspace, operation_id=operation_id)
    return {
        "adapter": adapter,
        "runner": runner,
        "materialization": materialization,
        "spec": spec,
        "workspace": workspace,
        "operation_id": operation_id,
        "handle": handle,
        "tmp_path": tmp_path,
    }


def test_verifier_exposes_only_v3_verification_capability(verifier_case: dict[str, Any]) -> None:
    profile = verifier_case["adapter"].probe({})

    assert profile.context_package_versions == (3,)
    assert profile.verification_versions == (1,)
    assert profile.code_result_versions == ()
    assert len(verifier_case["runner"].starts) == 1


def test_verifier_can_interrupt_demo_start_and_keep_real_stop_observation(
    verifier_case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = verifier_case["runner"]

    def stop(operation_id: UUID) -> CheckRunObservation:
        runner.stops.append(operation_id)
        current = runner.observations[operation_id]
        interrupted = replace(
            current,
            status=CheckRunStatus.INTERRUPTED,
            finished_at=NOW + timedelta(seconds=1),
            exit_code=None,
            failure_code="stopped_by_request",
        )
        runner.observations[operation_id] = interrupted
        return interrupted

    monkeypatch.setattr(runner, "stop", stop)
    adapter = LocalVerificationAdapter(
        runner=runner,
        check_specs={"unit": _check_spec()},
        workspace_locator=lambda manifest: verifier_case["tmp_path"],
        start_interruption=lambda _operation_id: True,
    )
    operation_id = uuid4()

    handle = adapter.start_session(
        verifier_case["spec"],
        verifier_case["workspace"],
        operation_id=operation_id,
    )

    observation = runner.inspect(operation_id)
    assert observation is not None
    assert observation.status is CheckRunStatus.INTERRUPTED
    assert observation.failure_code == "stopped_by_request"
    assert runner.stops == [operation_id]
    events = tuple(adapter.read_events(handle))
    assert [event.event_type for event in events] == [
        "session.started",
        "session.canceled",
    ]


def test_verifier_rejects_normal_agent_input(verifier_case: dict[str, Any]) -> None:
    response = verifier_case["adapter"].send_input(
        verifier_case["handle"],
        "please rewrite the code",
        (),
        operation_id=uuid4(),
    )

    assert response.outcome is DeliveryOutcome.FAILED
    assert response.failure_code == "verification_input_unsupported"
    assert len(verifier_case["runner"].starts) == 1


def test_verifier_same_operation_start_is_idempotent(verifier_case: dict[str, Any]) -> None:
    second = verifier_case["adapter"].start_session(
        verifier_case["spec"],
        verifier_case["workspace"],
        operation_id=verifier_case["operation_id"],
    )

    assert second == verifier_case["handle"]
    assert len(verifier_case["runner"].starts) == 1


def test_verifier_revalidates_same_operation_before_replay(
    verifier_case: dict[str, Any],
) -> None:
    invalid_spec = replace(
        verifier_case["spec"],
        physical_input_ack={
            **verifier_case["spec"].physical_input_ack,
            "candidate_hash": "0" * 64,
        },
    )

    with pytest.raises(
        LocalVerificationAdapterError, match="verification_input_ack_mismatch"
    ):
        verifier_case["adapter"].start_session(
            invalid_spec,
            verifier_case["workspace"],
            operation_id=verifier_case["operation_id"],
        )

    assert len(verifier_case["runner"].starts) == 1


def test_verifier_events_identify_provider_purpose_and_terminal_observation(
    verifier_case: dict[str, Any],
) -> None:
    adapter = verifier_case["adapter"]
    runner = verifier_case["runner"]
    handle = verifier_case["handle"]
    operation_id = verifier_case["operation_id"]

    started = tuple(adapter.read_events(handle))
    assert started[0].event_type == "session.started"
    assert started[0].payload["provider"] == "local_verification"
    assert started[0].payload["purpose"] == "verification"

    runner.finish(operation_id, CheckRunStatus.PASSED)
    finished = tuple(adapter.read_events(handle))

    assert finished[0].event_type == "session.succeeded"
    assert finished[0].payload["status"] == "passed"
    assert finished[0].payload["actual_commit"] == COMMIT
    assert finished[0].payload["provider"] == "local_verification"


def test_verifier_stop_preserves_unknown_outcome(verifier_case: dict[str, Any]) -> None:
    runner = verifier_case["runner"]
    operation_id = verifier_case["operation_id"]
    runner.observations[operation_id] = replace(
        runner.observations[operation_id],
        status=CheckRunStatus.UNKNOWN,
        failure_code="process_state_unknown",
    )

    result = verifier_case["adapter"].stop_session(
        verifier_case["handle"],
        operation_id=uuid4(),
    )

    assert result.outcome is DeliveryOutcome.UNKNOWN
    assert result.failure_code == "process_state_unknown"
    assert runner.stops == [operation_id]


def test_verifier_stop_runner_error_is_unknown_and_idempotent(
    verifier_case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_operation_id = uuid4()

    def fail(_operation_id: UUID) -> None:
        raise OSError("stop unavailable")

    monkeypatch.setattr(verifier_case["runner"], "stop", fail)
    first = verifier_case["adapter"].stop_session(
        verifier_case["handle"], operation_id=stop_operation_id
    )
    second = verifier_case["adapter"].stop_session(
        verifier_case["handle"], operation_id=stop_operation_id
    )

    assert first == second
    assert first.outcome is DeliveryOutcome.UNKNOWN
    assert first.failure_code == "verification_stop_unknown"


def test_verifier_chat_patch_and_handoff_are_explicitly_unsupported(
    verifier_case: dict[str, Any],
) -> None:
    patch_result = verifier_case["adapter"].apply_context_patch(
        verifier_case["handle"],
        object(),  # type: ignore[arg-type]
        operation_id=uuid4(),
    )

    assert patch_result.outcome == "failed"
    assert patch_result.error_code == "verification_input_unsupported"
    with pytest.raises(LocalVerificationAdapterError, match="verification_handoff_unsupported"):
        verifier_case["adapter"].capture_handoff_checkpoint(
            verifier_case["handle"],
            transition_id=uuid4(),
            source_input_cutoff_sequence=0,
            operation_id=uuid4(),
        )


def test_verifier_rejects_non_verification_context_without_starting_runner(
    verifier_case: dict[str, Any],
) -> None:
    legacy = _materialization_v1()

    with pytest.raises(LocalVerificationAdapterError, match="verification_context_unsupported"):
        verifier_case["adapter"].accept_context(legacy, operation_id=uuid4())

    assert len(verifier_case["runner"].starts) == 1
