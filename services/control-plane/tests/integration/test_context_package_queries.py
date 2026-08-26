from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ContextAssemblyRequestModel,
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningBriefRevisionModel,
    PlanningProfileModel,
    PlanningRequestModel,
    PlanningRunModel,
)
from sidera_control_plane.persistence.work_context_repository import (
    WorkContextRepository,
)

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, local_actor_id="operator")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _command(
    workspace_id: UUID,
    command_type: str,
    payload: dict[str, object],
    *,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    key: str,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "command_type": command_type,
        "target_type": target_type,
        "actor": {"type": "local", "id": "operator"},
        "target_id": str(target_id),
        "expected_version": expected_version,
        "idempotency_key": key,
        "correlation_id": str(uuid4()),
        "payload": payload,
    }


def _snapshot() -> WorkContextSnapshotV1:
    return WorkContextSnapshotV1.create(
        objective="Query context package",
        constraints=(),
        acceptance_criteria=(),
        requested_outputs=(),
        clarification_decisions=(),
        accepted_decisions=(),
        unresolved_questions=(),
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        checkpoint_refs=(),
    )


def _seed_planning_context(
    database: Database, workspace_id: UUID
) -> tuple[UUID, UUID, UUID, UUID]:
    definition_id = uuid4()
    module_id = uuid4()
    profile_id = uuid4()
    planning_request_id = uuid4()
    context_id = uuid4()
    revision_id = uuid4()
    run_id = uuid4()
    profile_snapshot = {
        "profile_id": str(profile_id),
        "workspace_id": str(workspace_id),
        "profile_key": "query-profile",
        "profile_version": 1,
        "display_name": "Query profile",
        "planner_module_instance_id": str(module_id),
        "planner_module_config_version": 1,
        "planner_interface_version": "sidera.planner.v1",
        "prompt_policy_version": "prompt.v1",
        "system_policy": {},
        "proposal_schema_version": 2,
        "allowed_capabilities": ["code"],
        "adapter_preferences": ["fake"],
        "adapter_forbidden": [],
        "budget": {
            "max_context_bytes": 64_000,
            "max_tokens": 10_000,
            "max_cost_microunits": 1_000_000,
            "max_runtime_seconds": 600,
            "max_revisions": 3,
            "max_nodes": 20,
            "max_concurrency": 2,
        },
        "retry_policy": {
            "max_retries": 1,
            "backoff_seconds": [1],
            "unknown_outcome_policy": "block",
            "invalid_output_policy": "retry",
        },
        "default_priority": 50,
        "execution_max_attempts": 2,
        "execution_budget_level": "standard",
        "planner_secret_refs": [],
        "planner_capabilities": {
            "structured_output": "json_schema",
            "clarification": True,
            "idempotency": "operation-id",
            "cancellation": "run",
            "usage_reporting": "tokens",
            "max_context_bytes": 128_000,
            "stability": "stable",
            "interface_version": "sidera.planner.v1",
            "proposal_schema_versions": [2],
            "context_package_versions": [1],
        },
    }
    request = PlanningRequestModel(
        id=planning_request_id,
        workspace_id=workspace_id,
        kind="create",
        status="planning",
        current_brief_revision=None,
        current_proposal_revision=None,
        current_proposal_content_hash=None,
        planning_profile_id=profile_id,
        planning_profile_version=1,
        profile_snapshot=profile_snapshot,
        source_type="integration",
        source_ref="context-package-query",
        actor="integration",
        correlation_id=uuid4(),
        version=1,
    )
    revision = WorkContextRevision.create(
        revision_id=revision_id,
        work_context_id=context_id,
        workspace_id=workspace_id,
        revision_number=1,
        brief_planning_request_id=planning_request_id,
        brief_revision=1,
        proposal_revision_id=None,
        plan_snapshot_id=None,
        reason=WorkContextRevisionReason.REQUEST_CREATED,
        snapshot=_snapshot(),
        created_by="integration",
        created_at=NOW,
    )
    context = WorkContext.create(
        work_context_id=context_id,
        workspace_id=workspace_id,
        planning_request_id=planning_request_id,
        first_revision=revision,
        created_at=NOW,
    )
    with database.session() as session:
        session.add(
            ModuleDefinitionModel(
                id=definition_id,
                workspace_id=workspace_id,
                module_type="planner_adapter",
                provider="fake",
                interface_version="sidera.planner.v1",
                config_schema_version=1,
                capabilities={},
                lifecycle="active",
            )
        )
        session.add(
            ModuleInstanceModel(
                id=module_id,
                workspace_id=workspace_id,
                definition_id=definition_id,
                instance_name=f"query-{module_id.hex[:8]}",
                config_version=1,
                config_namespace="planning",
                config={},
                secret_refs=[],
                lifecycle="active",
            )
        )
        session.add(
            PlanningProfileModel(
                id=profile_id,
                workspace_id=workspace_id,
                profile_key=f"query-{profile_id.hex[:8]}",
                version=1,
                display_name="Query profile",
                planner_module_instance_id=module_id,
                planner_module_config_version=1,
                planner_interface_version="sidera.planner.v1",
                status="active",
                user_selectable=True,
                policy={},
            )
        )
        session.add(request)
        session.flush()
        session.add(
            PlanningBriefRevisionModel(
                planning_request_id=planning_request_id,
                revision=1,
                workspace_id=workspace_id,
                payload={"objective": "Query context package"},
                created_by="integration",
                created_at=NOW,
            )
        )
        session.flush()
        request.current_brief_revision = 1
        session.flush()
        WorkContextRepository(session).add(context, revision)
        session.add(
            PlanningRunModel(
                id=run_id,
                workspace_id=workspace_id,
                planning_request_id=planning_request_id,
                brief_revision=1,
                planning_profile_id=profile_id,
                planning_profile_version=1,
                planner_module_instance_id=module_id,
                planner_module_config_version=1,
                attempt=1,
                status="queued",
                result_kind=None,
                capability_snapshot={},
                usage={
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_microunits": 0,
                    "runtime_seconds": 0,
                    "context_bytes": 0,
                },
                operation_id=uuid4(),
                provider_ref=None,
                error_code=None,
                error_message=None,
                diagnostic_refs=[],
                queued_at=NOW,
                started_at=None,
                finished_at=None,
                version=1,
                updated_at=NOW,
            )
        )
        session.commit()
    return planning_request_id, context_id, revision_id, run_id


def _seed_package(
    database: Database, workspace_id: UUID
) -> tuple[UUID, UUID, str, UUID]:
    planning_request_id, _context_id, revision_id, consumer_id = _seed_planning_context(
        database, workspace_id
    )
    request_id = uuid4()
    assembly = ContextAssemblyRequest.create(
        request_id=request_id,
        workspace_id=workspace_id,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=consumer_id,
        worker_item_id=None,
        work_context_revision_id=revision_id,
        context_version=None,
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
        expires_at=NOW + timedelta(hours=1),
        request_fingerprint="a" * 64,
        idempotency_key=f"context-query:{request_id}",
        created_at=NOW,
    )
    manifest = ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=workspace_id,
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=consumer_id,
        worker_item_id=None,
        work_context_revision_id=revision_id,
        context_version=None,
        control_plane_fact_snapshot={"planning_request_id": str(planning_request_id)},
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
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=assembly.idempotency_key,
        created_at=NOW,
    )
    with database.session() as session:
        repository = ContextAssemblyRepository(session)
        repository.add(assembly)
        claimed = repository.claim_next(
            workspace_id=workspace_id,
            owner="query-test",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        repository.complete_with_package(
            claimed,
            manifest,
            owner="query-test",
            expected_version=claimed.version,
            actor_type="system",
            actor_id="query-test",
            causation_id=uuid4(),
            correlation_id=uuid4(),
        )
        session.commit()
    return manifest.package_id, request_id, manifest.package_hash, consumer_id


def test_context_package_manifest_materialize_and_workspace_scope(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    package_id, request_id, package_hash, _consumer_id = _seed_package(
        database, workspace_id
    )

    manifest = client.get(
        f"/api/v1/workspaces/{workspace_id}/context-packages/{package_id}"
    )
    assert manifest.status_code == 200, manifest.text
    payload = manifest.json()
    assert payload["package_hash"] == package_hash
    assert "body_markdown" not in manifest.text
    assert payload["knowledge_refs"] == []
    assert payload["acknowledgements"] == []
    assert payload["materialization_audits"] == []

    by_run = client.get(
        f"/api/v1/workspaces/{workspace_id}/planning-runs/{_consumer_id}/context-package"
    )
    assert by_run.status_code == 200, by_run.text
    assert by_run.json()["package_id"] == str(package_id)

    materialized = client.post(
        f"/api/v1/workspaces/{workspace_id}/context-packages/{package_id}/materialize"
    )
    assert materialized.status_code == 200, materialized.text
    assert materialized.headers["cache-control"] == "no-store"
    assert materialized.json()["metadata"]["package_id"] == str(package_id)

    assert (
        client.get(
            f"/api/v1/workspaces/{uuid4()}/context-packages/{package_id}"
        ).status_code
        == 404
    )
    assert request_id


def test_retry_and_revoke_commands_are_owner_and_fingerprint_scoped(
    client: TestClient, database: Database, workspace_id: UUID
) -> None:
    package_id, request_id, package_hash, _consumer_id = _seed_package(
        database, workspace_id
    )
    with database.session() as session:
        session.query(ContextAssemblyRequestModel).filter_by(id=request_id).update(
            {"status": "blocked", "error_code": "context_reference_not_found"}
        )
        session.commit()

    retried = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RetryContextAssembly",
            {"request_fingerprint": "a" * 64},
            target_type="context_assembly",
            target_id=request_id,
            expected_version=3,
            key="retry-context-query",
        ),
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["context_assembly_request_id"] == str(request_id)

    revoked = client.post(
        "/api/v1/commands",
        json=_command(
            workspace_id,
            "RevokeContextPackage",
            {
                "expected_package_status": "active",
                "expected_package_hash": package_hash,
                "revocation_scope": "new_starts",
                "reason": "operator_request",
            },
            target_type="context_package",
            target_id=package_id,
            expected_version=1,
            key="revoke-context-query",
        ),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["package_status"] == "revoked"
