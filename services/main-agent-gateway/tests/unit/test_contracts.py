from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints

import pytest

from sidera_main_agent_gateway.contracts import (
    MainAgentAdapter,
    MainAgentEvent,
    MainAgentItem,
    MainAgentThread,
    MainAgentThreadPage,
    MainAgentTurn,
    MainAgentTurnHandle,
)
from sidera_main_agent_gateway.errors import (
    ProviderProtocolError,
    ProviderUnavailable,
    ThreadAccessDenied,
    ThreadBusy,
    ThreadNotFound,
    TurnNotFound,
    TurnStreamUnavailable,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def test_thread_and_turn_models_are_immutable_and_provider_neutral() -> None:
    thread = MainAgentThread(
        id="thread-1",
        title="Build gateway",
        created_at=NOW,
        updated_at=NOW,
        context_refs=("task:123",),
    )
    item = MainAgentItem(
        id="item-1",
        type="assistantMessage",
        data={"text": "hello", "metadata": {"attempt": 1}},
    )
    turn = MainAgentTurn(
        id="turn-1",
        thread_id=thread.id,
        status="completed",
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
        final_response="hello",
        items=(item,),
        usage={"inputTokens": 3},
        error=None,
    )

    assert thread.context_refs == ("task:123",)
    assert turn.items == (item,)
    with pytest.raises(AttributeError):
        thread.title = "changed"  # type: ignore[misc]


def test_models_reject_naive_datetimes_unknown_states_and_invalid_json() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MainAgentThread(
            id="thread-1",
            title="Build gateway",
            created_at=datetime(2026, 8, 24, 12, 0),  # noqa: DTZ001
            updated_at=NOW,
            context_refs=(),
        )

    with pytest.raises(ValueError, match="item type"):
        MainAgentItem(id="item-1", type="unknown", data={})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="JSON-compatible"):
        MainAgentItem(
            id="item-1",
            type="toolCall",
            data={"bad": object()},  # type: ignore[dict-item]
        )

    with pytest.raises(ValueError, match="turn status"):
        MainAgentTurn(
            id="turn-1",
            thread_id="thread-1",
            status="running",  # type: ignore[arg-type]
            created_at=NOW,
            started_at=None,
            completed_at=None,
            final_response=None,
            items=(),
            usage=None,
            error=None,
        )


def test_event_and_page_models_validate_json_payloads() -> None:
    event = MainAgentEvent(
        sequence=1,
        type="item",
        data={"item": {"id": "item-1", "parts": ["a", 2]}},
    )
    page = MainAgentThreadPage(
        threads=(
            MainAgentThread(
                id="thread-1",
                title="Build gateway",
                created_at=NOW,
                updated_at=NOW,
                context_refs=(),
            ),
        ),
        next_cursor="next",
    )

    assert event.sequence == 1
    assert page.next_cursor == "next"
    with pytest.raises(ValueError, match="sequence"):
        MainAgentEvent(sequence=0, type="started", data={})


def test_protocols_expose_provider_neutral_gateway_operations() -> None:
    adapter_methods = set(vars(MainAgentAdapter))
    handle_methods = set(vars(MainAgentTurnHandle)) | set(
        get_type_hints(MainAgentTurnHandle)
    )

    assert {
        "health",
        "capabilities",
        "list_threads",
        "create_thread",
        "read_thread",
        "start_turn",
        "read_turn",
        "interrupt_turn",
        "close",
    } <= set(adapter_methods)
    assert {"thread_id", "id", "events", "result", "interrupt"} <= set(
        handle_methods
    )


def test_error_hierarchy_keeps_gateway_error_categories_stable() -> None:
    assert issubclass(ProviderUnavailable, Exception)
    assert issubclass(ProviderProtocolError, Exception)
    assert issubclass(ThreadNotFound, Exception)
    assert issubclass(ThreadAccessDenied, Exception)
    assert issubclass(ThreadBusy, Exception)
    assert issubclass(TurnNotFound, Exception)
    assert issubclass(TurnStreamUnavailable, Exception)


def test_neutral_modules_do_not_import_provider_sdk() -> None:
    source_root = Path(__file__).parents[2] / "src" / "sidera_main_agent_gateway"
    paths = [
        source_root / "contracts.py",
        source_root / "errors.py",
        *sorted((source_root / "api").glob("*.py")),
    ]
    for path in paths:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert all(
            module != "akashic_sdk" and not module.startswith("akashic_sdk.")
            for module in imports
            if module is not None
        ), path
