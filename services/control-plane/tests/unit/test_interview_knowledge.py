from __future__ import annotations

from uuid import uuid4


def test_demo_knowledge_candidate_is_typed_and_binds_the_source_artifact() -> None:
    from sidera_control_plane.demo.knowledge import demo_knowledge_candidate

    worker_item_id = uuid4()
    artifact_id = uuid4()
    proposal = demo_knowledge_candidate(worker_item_id, artifact_id)

    assert proposal.operation == "create_page"
    assert proposal.title == "样例项目：手机号允许为空"
    assert proposal.artifact_refs == (str(artifact_id),)
    assert proposal.source_suggestions[0].source_type == "artifact"
    assert proposal.source_suggestions[0].source_id == artifact_id
    assert proposal.source_suggestions[0].metadata == {"artifact_id": str(artifact_id)}
    assert "用户偏好" not in proposal.sections[0].body_markdown


def test_demo_knowledge_candidate_is_deterministically_reusable() -> None:
    from sidera_control_plane.demo.knowledge import demo_knowledge_candidate

    worker_item_id = uuid4()
    artifact_id = uuid4()
    first = demo_knowledge_candidate(worker_item_id, artifact_id)
    second = demo_knowledge_candidate(worker_item_id, artifact_id)

    assert first.to_mapping() == second.to_mapping()
    assert first.proposal_hash == second.proposal_hash
