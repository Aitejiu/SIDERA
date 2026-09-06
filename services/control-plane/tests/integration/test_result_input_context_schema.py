from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, update

from sidera_control_plane.domain.context_packages import (
    ContextAssemblyRequest,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPurpose,
    ResultInputBudgetV1,
    ResultInputManifestV1,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
    ContextPackageRepository,
)
from sidera_control_plane.persistence.models import ContextPackageModel
from tests.integration.test_context_package_repository import _seed_context
from tests.integration.test_input_selection_repository import (
    seed_selection_dependencies,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 6, 13, 0, tzinfo=UTC)


def _context_budget() -> ContextBudgetV1:
    return ContextBudgetV1(
        profile_id=UUID("72000000-0000-0000-0000-000000000001"),
        profile_version=1,
        max_sections=8,
        max_chars=20_000,
        max_input_tokens=4_000,
    )


def _assembly(
    *,
    workspace_id: UUID,
    revision_id: UUID,
    consumer_id: UUID,
    request_id: UUID,
    idempotency_key: str,
    protocol: int,
    worker_item_id: UUID,
    selection_id: UUID | None = None,
    selection_hash: str | None = None,
) -> ContextAssemblyRequest:
    return ContextAssemblyRequest.create(
        request_id=request_id,
        workspace_id=workspace_id,
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=consumer_id,
        worker_item_id=worker_item_id,
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
        expires_at=NOW + timedelta(hours=1),
        request_fingerprint=("a" if protocol == 1 else "b") * 64,
        idempotency_key=idempotency_key,
        created_at=NOW,
        manifest_schema_version=protocol,
        input_selection_id=(selection_id if protocol == 2 else None),
        input_selection_hash=(selection_hash if protocol == 2 else None),
        result_input_budget=(ResultInputBudgetV1() if protocol == 2 else None),
    )


def test_context_protocol_columns_constraints_round_trip_and_isolation(
    database, workspace_id: UUID, uow_factory
) -> None:
    _request_id, revision_id, _context_id, _module_id = _seed_context(
        database, workspace_id
    )
    selection = seed_selection_dependencies(database, workspace_id)
    with uow_factory() as uow:
        uow.input_selections.add(selection)

    consumer_id = uuid4()
    v1_request = _assembly(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
        request_id=uuid4(),
        idempotency_key="context-schema-v1",
        protocol=1,
        worker_item_id=selection.target_worker_item_id,
    )
    v2_request = _assembly(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
        request_id=uuid4(),
        idempotency_key="context-schema-v2",
        protocol=2,
        worker_item_id=selection.target_worker_item_id,
        selection_id=selection.id,
        selection_hash=selection.selection_hash,
    )
    v1_manifest = ContextPackageManifestV1.create(
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
        budget=_context_budget(),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=v1_request.idempotency_key,
        created_at=NOW,
    )
    v2_manifest = ContextPackageManifestV2.create(
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
        budget=_context_budget(),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=v2_request.idempotency_key,
        created_at=NOW,
        result_input_manifest=ResultInputManifestV1.from_selection(selection),
    )

    with database.session() as session:
        assemblies = ContextAssemblyRepository(session)
        assemblies.add(v1_request)
        assemblies.add(v2_request)
        claimed = assemblies.claim_next(
            workspace_id=workspace_id,
            owner="context-schema-test",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=2,
        )
        assert {item.manifest_schema_version for item in claimed} == {1, 2}
        for request in claimed:
            manifest = v1_manifest if request.manifest_schema_version == 1 else v2_manifest
            assemblies.complete_with_package(
                request,
                manifest,
                owner="context-schema-test",
                expected_version=request.version,
                actor_type="system",
                actor_id="context-schema-test",
                causation_id=uuid4(),
                correlation_id=uuid4(),
            )
        session.commit()

    with database.session() as session:
        packages = ContextPackageRepository(session)
        loaded_v1 = packages.get_for_consumer(
            ContextPurpose.EXECUTION, consumer_id, workspace_id
        )
        loaded_v2 = packages.get_for_consumer(
            ContextPurpose.EXECUTION,
            consumer_id,
            workspace_id,
            manifest_schema_version=2,
        )
        assert isinstance(loaded_v1, ContextPackageManifestV1)
        assert not isinstance(loaded_v1, ContextPackageManifestV2)
        assert isinstance(loaded_v2, ContextPackageManifestV2)
        assert loaded_v2.result_input_manifest.input_selection_hash == selection.selection_hash

        session.execute(
            update(ContextPackageModel)
            .where(ContextPackageModel.id == loaded_v2.package_id)
            .values(package_hash="f" * 64)
        )
        session.flush()
        with pytest.raises(ValidationError, match="stored context package hash"):
            packages.get(loaded_v2.package_id, workspace_id)

        columns = {
            item["name"] for item in inspect(session.bind).get_columns("context_assembly_requests")
        }
        assert {
            "manifest_schema_version",
            "input_selection_id",
            "input_selection_hash",
            "result_input_budget",
        }.issubset(columns)
        package_columns = {
            item["name"] for item in inspect(session.bind).get_columns("context_packages")
        }
        assert {"manifest_schema_version", "result_input_manifest"}.issubset(package_columns)
        assert "uq_context_packages_purpose_consumer_protocol" in {
            item["name"] for item in inspect(session.bind).get_unique_constraints("context_packages")
        }
