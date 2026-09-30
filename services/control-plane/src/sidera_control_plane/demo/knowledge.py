from __future__ import annotations

from uuid import UUID

from sidera_control_plane.application.knowledge_candidate_submission import (
    KnowledgeCandidateProposalV1,
    KnowledgeCandidateSectionV1,
    KnowledgeCandidateSourceSuggestionV1,
)


def demo_knowledge_candidate(
    worker_item_id: UUID, source_artifact_id: UUID
) -> KnowledgeCandidateProposalV1:
    """Return the fixed, provider-neutral knowledge proposal used by the demo."""
    if not isinstance(worker_item_id, UUID) or not isinstance(source_artifact_id, UUID):
        raise TypeError("demo knowledge candidate identities must be UUIDs")
    return KnowledgeCandidateProposalV1(
        operation="create_page",
        target_page_id=None,
        expected_revision_id=None,
        expected_revision_hash=None,
        title="样例项目：手机号允许为空",
        slug="sample-project-phone-rule",
        summary="样例 CSV 导入项目的手机号校验规则。",
        tags=("demo", "csv", "validation"),
        sections=(
            KnowledgeCandidateSectionV1(
                section_key="phone-rule",
                heading="手机号校验规则",
                summary="手机号字段允许为空；有值时才校验格式。",
                body_markdown=(
                    "手机号字段允许为空；当字段有值时，按项目约定校验其格式。\n\n"
                    "这条规则来自样例项目的校验产物，仅用于演示知识候选审核与后续上下文引用。"
                ),
                ordinal=0,
            ),
        ),
        source_suggestions=(
            KnowledgeCandidateSourceSuggestionV1(
                source_type="artifact",
                label="样例项目校验产物",
                source_id=source_artifact_id,
                metadata={"artifact_id": str(source_artifact_id)},
            ),
        ),
        artifact_refs=(str(source_artifact_id),),
        evidence_refs=(),
        relations=(),
    )
