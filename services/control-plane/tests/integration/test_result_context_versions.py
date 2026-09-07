from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters.opencode_acp import OpenCodeACPAdapter
from sidera_control_plane.domain.code_results import CodeInputManifestV1
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextMaterializationV3,
    ContextPackageManifestV3,
    ContextPurpose,
    context_package_reference,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.workspace import (
    BranchPolicy,
    CheckoutMode,
    ExecutionSpec,
    RepositoryCheckoutSpec,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
    ContextPackageRepository,
)
from sidera_control_plane.persistence.models import ContextAssemblyRequestModel
from tests.integration.test_context_package_repository import (
    _event_context,
    _seed_context,
)
from tests.integration.test_input_selection_repository import (
    seed_selection_dependencies,
)
from tests.unit.result_integration_fixtures import (
    code_input_manifest_mapping,
    context_v3_mapping,
    verification_manifest_mapping,
)
from tests.unit.test_context_package_v2 import _materialization_v1

pytestmark = pytest.mark.integration


def test_v3_manifest_round_trip_preserves_exact_code_input() -> None:
    raw = context_v3_mapping(mode="code")

    manifest = ContextPackageManifestV3.from_mapping(raw)

    assert manifest.to_mapping() == raw
    assert manifest.manifest_schema_version == 3
    assert manifest.code_input_manifest is not None
    assert manifest.verification_manifest is None


def test_v3_cannot_mix_code_and_verification_inputs() -> None:
    value = context_v3_mapping(mode="code")
    value["verification_manifest"] = verification_manifest_mapping()

    with pytest.raises(ValidationError, match="exactly one"):
        ContextPackageManifestV3.from_mapping(value)


def test_v3_rejects_non_worker_consumer() -> None:
    value = context_v3_mapping(mode="verification", consumer_type="planner")

    with pytest.raises(ValidationError, match="execution package must use worker"):
        ContextPackageManifestV3.from_mapping(value)


def test_execution_request_rejects_unknown_result_derivation() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="result derivation"):
        ExecutionRequest.create(
            request_id=uuid4(),
            workspace_id=uuid4(),
            planning_request_id=uuid4(),
            proposal_revision_id=uuid4(),
            plan_snapshot_id=uuid4(),
            plan_generation=1,
            root_task_id=uuid4(),
            subtask_id=uuid4(),
            worker_item_id=uuid4(),
            worker_item_key="worker",
            intent_key="worker:first",
            purpose="execute",
            capability_requirements=("structured_output",),
            adapter_preferences=("fake",),
            budget_class="standard",
            not_before=now,
            max_attempts=1,
            expected_artifact_refs=(),
            expected_evidence_refs=(),
            expected_outputs=("result",),
            now=now,
            result_derivation_kind="unknown",
        )


def test_execution_spec_v3_binds_purpose_and_physical_input_ack() -> None:
    legacy = _materialization_v1()
    execution_id = uuid4()
    materialization = ContextMaterializationV3(
        metadata=legacy.metadata,
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        result_inputs=(),
        code_input_manifest=CodeInputManifestV1.from_mapping(
            context_v3_mapping(mode="code")["code_input_manifest"]
        ),
        verification_manifest=None,
    )
    spec = ExecutionSpec(
        schema_version=3,
        workspace_id=uuid4(),
        task_id=uuid4(),
        execution_id=execution_id,
        worker_id=uuid4(),
        lease_id=uuid4(),
        fencing_token=1,
        goal="Run the integration worker",
        acceptance_criteria=("the candidate is captured",),
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
        sandbox_policy={},
        budget={},
        deadline=None,
        expected_outputs=("code result",),
        adapter_module_ref=uuid4(),
        adapter_config_version=1,
        workspace_module_ref=uuid4(),
        workspace_config_version=1,
        context_package=materialization,
        execution_purpose="code",
        physical_input_ack={"execution_id": str(execution_id), "input_hash": "6" * 64},
    )

    assert spec.schema_version == 3
    assert spec.execution_purpose == "code"

    with pytest.raises(ValidationError, match="execution purpose"):
        replace(spec, execution_purpose="verification")


def test_legacy_opencode_adapter_rejects_v3_until_capability_is_declared() -> None:
    legacy = _materialization_v1()
    materialization = ContextMaterializationV3(
        metadata=legacy.metadata,
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        result_inputs=(),
        code_input_manifest=CodeInputManifestV1.from_mapping(
            context_v3_mapping(mode="code")["code_input_manifest"]
        ),
        verification_manifest=None,
    )

    with pytest.raises((TypeError, ValueError), match="materialization schema"):
        OpenCodeACPAdapter(executable="/missing/opencode").accept_context(
            materialization,
            operation_id=UUID("80000000-0000-0000-0000-000000000001"),
        )


def test_v3_package_persists_and_reloads_with_exact_code_manifest(
    database, workspace_id: UUID
) -> None:
    _request_id, revision_id, _context_id, _module_id = _seed_context(
        database, workspace_id
    )
    selection = seed_selection_dependencies(database, workspace_id)
    consumer_id = uuid4()
    code_manifest = CodeInputManifestV1.from_mapping(code_input_manifest_mapping())
    request = ContextAssemblyRequest.create(
        request_id=uuid4(),
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=consumer_id,
        worker_item_id=selection.target_worker_item_id,
        work_context_revision_id=revision_id,
        context_version=1,
        knowledge_search_scope={},
        knowledge_filter_refs=(),
        fact_scope_refs=(),
        budget={
            "profile_id": str(uuid4()),
            "profile_version": 1,
            "max_sections": 8,
            "max_chars": 20_000,
            "max_input_tokens": 4_000,
        },
        expires_at=datetime(2026, 9, 7, 13, 0, tzinfo=UTC),
        request_fingerprint="c" * 64,
        idempotency_key="context-v3-persist",
        created_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        manifest_schema_version=3,
        code_input_manifest=code_manifest,
    )
    manifest = ContextPackageManifestV3.create(
        package_id=uuid4(),
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=consumer_id,
        worker_item_id=selection.target_worker_item_id,
        work_context_revision_id=revision_id,
        context_version=1,
        control_plane_fact_snapshot={},
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=uuid4(),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
        ),
        expires_at=datetime(2026, 9, 7, 13, 0, tzinfo=UTC),
        idempotency_key=request.idempotency_key,
        created_at=request.created_at,
        code_input_manifest=code_manifest,
    )

    with database.session() as session:
        assemblies = ContextAssemblyRepository(session)
        assemblies.add(request)
        claimed = assemblies.claim_next(
            workspace_id=workspace_id,
            owner="context-v3-test",
            now=request.created_at,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )
        assert len(claimed) == 1
        assemblies.complete_with_package(
            claimed[0],
            manifest,
            owner="context-v3-test",
            expected_version=claimed[0].version,
            **_event_context(session, workspace_id),
        )
        session.commit()

    with database.session() as session:
        loaded = ContextPackageRepository(session).get_for_consumer(
            ContextPurpose.EXECUTION,
            consumer_id,
            workspace_id,
            manifest_schema_version=3,
        )
        assert isinstance(loaded, ContextPackageManifestV3)
        assert loaded.code_input_manifest == code_manifest
        assert loaded.verification_manifest is None
        assert loaded.package_hash == manifest.package_hash
        assert session.get(ContextAssemblyRequestModel, request.request_id) is not None
