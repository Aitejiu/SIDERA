from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from sidera_knowledge.application.knowledge_commands import (
    SubmitKnowledgeCandidateHandler,
)
from sidera_knowledge.domain.commands import CommandEnvelope
from sidera_knowledge.domain.knowledge_errors import CommandFailure


def _payload(**overrides):
    payload = {
        "candidate_schema_version": 1,
        "candidate_id": str(uuid4()),
        "operation": "create_page",
        "title": "Project Guide",
        "slug": "project-guide",
        "summary": "A guide",
        "tags": ["sidera"],
        "sections": [
            {
                "section_key": "overview",
                "heading": "Overview",
                "summary": "Summary",
                "body_markdown": "body",
                "ordinal": 0,
            }
        ],
        "relations": [],
        "sources": [
            {
                "source_id": str(uuid4()),
                "source_type": "manual",
                "label": "Owner note",
                "metadata": {},
            }
        ],
        "provenance": {"producer_type": "main_agent", "producer_id": "agent-1"},
    }
    payload.update(overrides)
    return payload


def _command(payload, actor_type="main_agent"):
    return CommandEnvelope(
        command_id=uuid4(),
        workspace_id=uuid4(),
        command_type="SubmitKnowledgeCandidate",
        actor_type=actor_type,
        actor_id="agent-1",
        target_type="knowledge_candidate",
        target_id=UUID(payload["candidate_id"]),
        payload=payload,
        expected_version=None,
        idempotency_key=uuid4().hex,
        correlation_id=uuid4(),
    )


class FakeSources:
    def __init__(self):
        self.items = []

    def find_by_digest(self, workspace_id, source_type, digest):
        return None

    def add_source(self, source):
        self.items.append(source)


class FakeCandidates:
    def __init__(self):
        self.items = []

    def add_candidate(self, candidate):
        self.items.append(candidate)


class FakeEvents:
    def __init__(self):
        self.items = []

    def append_with_outbox(self, event):
        self.items.append(event)


class FakeUow:
    def __init__(self):
        self.sources = FakeSources()
        self.candidates = FakeCandidates()
        self.events = FakeEvents()

    def flush(self):
        return None


def test_submit_handler_creates_pending_candidate_and_safe_event() -> None:
    payload = _payload()
    command = _command(payload)
    uow = FakeUow()

    result = SubmitKnowledgeCandidateHandler().handle(command, uow)

    candidate = uow.candidates.items[0]
    assert result == {"candidate_id": str(candidate.candidate_id), "status": "pending"}
    assert candidate.status == "pending"
    assert candidate.proposed_sections[0].body_markdown == "body\n"
    assert len(uow.sources.items) == 1
    assert uow.events.items[0].event_type == "KnowledgeCandidateSubmitted"
    assert "body" not in str(uow.events.items[0].payload).lower()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"target_page_id": str(uuid4())}, "candidate_invalid"),
        ({"sources": [{"source_type": "markdown_file", "label": "bad", "workspace_relative_path": "/tmp/x"}]}, "source_invalid"),
    ],
)
def test_submit_handler_rejects_invalid_snapshot_or_source(payload, code) -> None:
    full = _payload(**payload)
    with pytest.raises(CommandFailure, match=code):
        SubmitKnowledgeCandidateHandler().handle(_command(full), FakeUow())


def test_control_plane_cannot_submit_manual_candidate() -> None:
    with pytest.raises(CommandFailure, match="permission_denied"):
        SubmitKnowledgeCandidateHandler().handle(
            _command(_payload(), actor_type="control_plane"),
            FakeUow(),
        )


def test_section_hard_limit_is_measured_in_utf8_bytes() -> None:
    body = "x" * (128 * 1024 + 1)
    with pytest.raises(CommandFailure, match="content_limit_exceeded"):
        SubmitKnowledgeCandidateHandler().handle(
            _command(
                _payload(
                    sections=[
                        {
                            "section_key": "large",
                            "heading": "Large",
                            "summary": "",
                            "body_markdown": body,
                            "ordinal": 0,
                        }
                    ]
                )
            ),
            FakeUow(),
        )
