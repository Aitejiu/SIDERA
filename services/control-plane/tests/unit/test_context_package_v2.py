from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.context_packages import (
    ContextBudgetV1,
    ContextConsumerType,
    ContextMaterializationV1,
    ContextMaterializationV2,
    ContextMetadata,
    ContextPackageManifestV1,
    ContextPackageManifestV2,
    ContextPurpose,
    ControlPlaneFactSection,
    MaterializedResultInput,
    ResultInputManifestV1,
    SystemConstraintSection,
    WorkContextSection,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.work_context import ContextTextItem
from tests.unit.result_input_fixtures import result_input_manifest, selection_mapping

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _budget() -> ContextBudgetV1:
    return ContextBudgetV1(
        profile_id=uuid4(),
        profile_version=1,
        max_sections=8,
        max_chars=20_000,
        max_input_tokens=4_000,
    )


def _v2_manifest(
    *,
    result_manifest: ResultInputManifestV1 | None = None,
    status: str = "active",
) -> ContextPackageManifestV2:
    return ContextPackageManifestV2(
        package_id=uuid4(),
        workspace_id=uuid4(),
        purpose=ContextPurpose.EXECUTION,
        consumer_type=ContextConsumerType.WORKER,
        consumer_id=uuid4(),
        worker_item_id=uuid4(),
        work_context_revision_id=uuid4(),
        context_version=1,
        control_plane_fact_snapshot={"execution_id": str(uuid4())},
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=_budget(),
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=f"context-v2-{uuid4()}",
        status=status,
        created_at=NOW,
        result_input_manifest=result_manifest or result_input_manifest(),
    )


def _materialization_v1() -> ContextMaterializationV1:
    package_id = uuid4()
    consumer_id = uuid4()
    return ContextMaterializationV1(
        metadata=ContextMetadata(
            package_id=package_id,
            package_hash="a" * 64,
            purpose=ContextPurpose.EXECUTION,
            consumer_type=ContextConsumerType.WORKER,
            consumer_id=consumer_id,
            context_version=1,
            expires_at=NOW + timedelta(hours=1),
        ),
        system_constraints=SystemConstraintSection(
            objective="Execute the task",
            constraints=(),
            acceptance_criteria=(
                ContextTextItem(key="done", text="The task is complete", ordinal=0),
            ),
        ),
        work_context=WorkContextSection(
            revision_id=uuid4(),
            revision_number=1,
            content_hash="b" * 64,
            snapshot_schema_version=1,
            requested_outputs=(),
            clarification_decisions=(),
            accepted_decisions=(),
            unresolved_questions=(),
            checkpoint_refs=(),
            missing_fact_codes=(),
        ),
        control_plane_facts=ControlPlaneFactSection(facts={"execution": "ready"}),
        project_knowledge=(),
        artifact_and_evidence_refs=(),
    )


def test_manifest_contains_fixed_references_but_no_body() -> None:
    manifest = result_input_manifest()
    raw = manifest.to_mapping()

    assert raw["schema_version"] == 1
    assert raw["delivery_mode"] == "bounded_text_v1"
    assert "body_text" not in json.dumps(raw)
    assert raw["input_selection_hash"] == selection_mapping()["selection_hash"]
    assert raw["budget"] == {
        "schema_version": 1,
        "max_artifact_bytes": 32_768,
        "max_total_artifact_bytes": 131_072,
        "max_artifact_count": 128,
        "max_payload_bytes": 262_144,
    }
    assert ResultInputManifestV1.from_mapping(raw) == manifest


def test_v1_package_hash_remains_legacy_literal() -> None:
    manifest = ContextPackageManifestV1.create(
        package_id=UUID("73000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("73000000-0000-0000-0000-000000000002"),
        purpose=ContextPurpose.PLANNING_RUN,
        consumer_type=ContextConsumerType.PLANNER,
        consumer_id=UUID("73000000-0000-0000-0000-000000000003"),
        worker_item_id=None,
        work_context_revision_id=UUID("73000000-0000-0000-0000-000000000004"),
        context_version=None,
        control_plane_fact_snapshot={
            "planning_request_id": "73000000-0000-0000-0000-000000000005"
        },
        knowledge_refs=(),
        artifact_refs=(),
        evidence_refs=(),
        budget=ContextBudgetV1(
            profile_id=UUID("73000000-0000-0000-0000-000000000006"),
            profile_version=1,
            max_sections=8,
            max_chars=20_000,
            max_input_tokens=4_000,
        ),
        expires_at=datetime(2026, 9, 6, 14, 0, tzinfo=UTC),
        idempotency_key="legacy-v1",
        created_at=datetime(2026, 9, 6, 13, 0, tzinfo=UTC),
    )

    assert manifest.package_hash == (
        "c1590b37587e6c2e98c95c3b595e18438a82ce6d38c24c6608e1f6abec6b8fd1"
    )
    assert manifest.manifest_schema_version == 1


def test_result_input_descriptors_keep_relations_and_provenance() -> None:
    manifest = result_input_manifest()

    assert len(manifest.artifacts) == 2
    assert {"research"} == set(manifest.artifacts[0].group_keys)
    assert manifest.artifacts[0].hard_refs == ()
    assert manifest.artifacts[0].sources[0].worker_item_key in {"a", "b"}
    assert len(manifest.evidence) == 2
    assert manifest.evidence[0].artifact_ids


def test_manifest_rejects_unknown_version_missing_selection_and_conflicting_hash() -> None:
    unknown = result_input_manifest().to_mapping()
    unknown["schema_version"] = 2
    with pytest.raises(ValidationError, match="unsupported result input manifest"):
        ResultInputManifestV1.from_mapping(unknown)

    with pytest.raises(ValidationError, match="selection"):
        ResultInputManifestV1.from_selection(None)  # type: ignore[arg-type]

    from sidera_control_plane.domain.input_selections import (
        CollaborationInputSelectionV1,
    )
    selection = CollaborationInputSelectionV1.from_mapping(selection_mapping())
    first_source = selection.groups[0].sources[0]
    second_source = selection.groups[0].sources[1]
    conflicting_artifact = replace(
        second_source.artifacts[0],
        artifact_id=first_source.artifacts[0].artifact_id,
        content_hash="f" * 64,
    )
    conflicting_source = replace(
        second_source,
        artifacts=(conflicting_artifact,),
        evidence=(
            replace(
                second_source.evidence[0],
                artifact_ids=(first_source.artifacts[0].artifact_id,),
            ),
        ),
    )
    selection = replace(
        selection,
        groups=(replace(selection.groups[0], sources=(first_source, conflicting_source)),),
    )
    with pytest.raises(ValidationError, match="artifact ID"):
        ResultInputManifestV1.from_selection(selection)


def test_v2_package_hash_binds_result_manifest_but_ignores_operational_state() -> None:
    first = _v2_manifest()
    changed_result = result_input_manifest().to_mapping()
    changed_result["budget"]["max_payload_bytes"] = 131_072  # type: ignore[index]
    changed = ResultInputManifestV1.from_mapping(changed_result)

    changed_manifest = ContextPackageManifestV2(
        package_id=first.package_id,
        workspace_id=first.workspace_id,
        purpose=first.purpose,
        consumer_type=first.consumer_type,
        consumer_id=first.consumer_id,
        worker_item_id=first.worker_item_id,
        work_context_revision_id=first.work_context_revision_id,
        context_version=first.context_version,
        control_plane_fact_snapshot=first.control_plane_fact_snapshot,
        knowledge_refs=first.knowledge_refs,
        artifact_refs=first.artifact_refs,
        evidence_refs=first.evidence_refs,
        budget=first.budget,
        expires_at=first.expires_at,
        idempotency_key=first.idempotency_key,
        status=first.status,
        created_at=first.created_at,
        result_input_manifest=changed,
    )
    assert changed_manifest.package_hash != first.package_hash

    operational = ContextPackageManifestV2(
        package_id=first.package_id,
        workspace_id=first.workspace_id,
        purpose=first.purpose,
        consumer_type=first.consumer_type,
        consumer_id=first.consumer_id,
        worker_item_id=first.worker_item_id,
        work_context_revision_id=first.work_context_revision_id,
        context_version=first.context_version,
        control_plane_fact_snapshot=first.control_plane_fact_snapshot,
        knowledge_refs=first.knowledge_refs,
        artifact_refs=first.artifact_refs,
        evidence_refs=first.evidence_refs,
        budget=first.budget,
        expires_at=first.expires_at,
        idempotency_key=first.idempotency_key,
        status="revoked",
        created_at=first.created_at,
        materialized_at=NOW + timedelta(minutes=1),
        result_input_manifest=first.result_input_manifest,
    )
    assert operational.package_hash == first.package_hash
    assert first.manifest_schema_version == 2


def test_materialization_v2_preserves_existing_sections_and_adds_bounded_input() -> None:
    legacy = _materialization_v1()
    artifact = result_input_manifest().artifacts[0]
    materialized = MaterializedResultInput(
        artifact_id=artifact.artifact_id,
        content_hash=artifact.content_hash,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        sources=artifact.sources,
        group_keys=artifact.group_keys,
        hard_refs=artifact.hard_refs,
        body_text="report-a\n",
        candidate_evidence=(),
    )
    value = ContextMaterializationV2(
        metadata=legacy.metadata,
        system_constraints=legacy.system_constraints,
        work_context=legacy.work_context,
        control_plane_facts=legacy.control_plane_facts,
        project_knowledge=legacy.project_knowledge,
        artifact_and_evidence_refs=legacy.artifact_and_evidence_refs,
        result_inputs=(materialized,),
    )

    raw = value.to_mapping()
    assert value.schema_version == 2
    assert raw["system_constraints"] == legacy.to_mapping()["system_constraints"]
    assert raw["result_inputs"][0]["body_text"] == "report-a\n"  # type: ignore[index]
    assert value.total_byte_count >= len(materialized.body_text)
