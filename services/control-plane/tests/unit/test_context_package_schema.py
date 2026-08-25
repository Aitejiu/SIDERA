from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextBudgetV1,
    ContextConsumerType,
    ContextPackageKnowledgeRef,
    ContextPackageManifestV1,
    ContextPackageStatus,
    ContextPurpose,
    ContextSelectionReason,
    ContextTrustLevel,
)
from sidera_control_plane.domain.errors import ValidationError

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _budget() -> ContextBudgetV1:
    return ContextBudgetV1(
        profile_id=uuid4(),
        profile_version=2,
        max_sections=8,
        max_chars=20_000,
        max_input_tokens=4_000,
        actual_sections=1,
        actual_chars=500,
        actual_input_tokens=100,
    )


def _knowledge_ref() -> ContextPackageKnowledgeRef:
    return ContextPackageKnowledgeRef(
        page_id=uuid4(),
        revision_id=uuid4(),
        revision_hash="a" * 64,
        section_key="overview",
        section_hash="b" * 64,
        source_ids=(uuid4(),),
        selection_reason=ContextSelectionReason.WORK_CONTEXT,
        trust_level=ContextTrustLevel.WORKSPACE_CONFIRMED,
        ordinal=0,
    )


def _planning_manifest() -> ContextPackageManifestV1:
    return ContextPackageManifestV1.create(
        package_id=uuid4(),
        workspace_id=uuid4(),
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=uuid4(),
        worker_item_id=None,
        work_context_revision_id=uuid4(),
        context_version=None,
        control_plane_fact_snapshot={"planning_request_id": str(uuid4())},
        knowledge_refs=(_knowledge_ref(),),
        artifact_refs=(),
        evidence_refs=(),
        budget=_budget(),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key="package-1",
        created_at=NOW,
    )


def test_package_hash_excludes_operational_status_materialization_and_ack() -> None:
    manifest = _planning_manifest()
    changed = replace(
        manifest,
        status=ContextPackageStatus.REVOKED,
        materialized_at=NOW + timedelta(minutes=1),
    )
    assert changed.package_hash == manifest.package_hash
    assert manifest.package_hash == manifest.compute_package_hash()

    changed_facts = replace(
        manifest,
        control_plane_fact_snapshot={"planning_request_id": "different"},
    )
    assert changed_facts.package_hash != manifest.package_hash

    acknowledgement = ContextAcknowledgement(
        package_id=manifest.package_id,
        package_hash=manifest.package_hash,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=manifest.consumer_id,
        context_version=None,
        adapter_module_id=uuid4(),
        adapter_config_version=1,
        acknowledged_at=NOW,
    )
    assert acknowledgement.package_hash == manifest.package_hash


def test_execution_requires_worker_item_and_context_version() -> None:
    common = {
        "package_id": uuid4(),
        "workspace_id": uuid4(),
        "purpose": ContextPurpose.EXECUTION,
        "consumer_type": ContextConsumerType.WORKER,
        "consumer_id": uuid4(),
        "work_context_revision_id": uuid4(),
        "control_plane_fact_snapshot": {},
        "knowledge_refs": (),
        "artifact_refs": (),
        "evidence_refs": (),
        "budget": _budget(),
        "expires_at": NOW + timedelta(hours=1),
        "idempotency_key": "execution-package",
        "created_at": NOW,
    }
    with pytest.raises(ValidationError, match="worker_item_id"):
        ContextPackageManifestV1.create(
            **common, worker_item_id=None, context_version=1
        )
    with pytest.raises(ValidationError, match="context_version"):
        ContextPackageManifestV1.create(
            **common, worker_item_id=uuid4(), context_version=None
        )

    manifest = ContextPackageManifestV1.create(
        **common, worker_item_id=uuid4(), context_version=3
    )
    assert manifest.consumer_type is ContextConsumerType.WORKER
    assert manifest.context_version == 3


def test_planning_rejects_worker_fields_and_refs_validate_exact_hashes() -> None:
    manifest = _planning_manifest()
    with pytest.raises(ValidationError, match="worker_item_id"):
        ContextPackageManifestV1.create(
            package_id=manifest.package_id,
            workspace_id=manifest.workspace_id,
            purpose=ContextPurpose.PLANNING_RUN,
            consumer_type=ContextConsumerType.PLANNER,
            consumer_id=manifest.consumer_id,
            worker_item_id=uuid4(),
            work_context_revision_id=manifest.work_context_revision_id,
            context_version=None,
            control_plane_fact_snapshot={},
            knowledge_refs=(),
            artifact_refs=(),
            evidence_refs=(),
            budget=_budget(),
            expires_at=manifest.expires_at,
            idempotency_key="invalid-planning",
            created_at=NOW,
        )
    with pytest.raises(ValidationError):
        ContextPackageKnowledgeRef(
            page_id=uuid4(),
            revision_id=uuid4(),
            revision_hash="not-a-hash",
            section_key="overview",
            section_hash="b" * 64,
            source_ids=(),
            selection_reason=ContextSelectionReason.EXPLICIT,
            trust_level=ContextTrustLevel.EXTERNAL_REFERENCE,
            ordinal=0,
        )
