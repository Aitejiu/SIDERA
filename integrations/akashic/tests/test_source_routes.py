from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from agent.tools.base import ToolExecutionContext

from sidera_task_bridge.source_routes import (
    MissingToolContext,
    SourceContextResolver,
)
from sidera_task_bridge.state import PrivateRoute


def private_context() -> ToolExecutionContext:
    return cast(
        ToolExecutionContext,
        SimpleNamespace(
        origin_channel="telegram",
        origin_chat_id="chat-42",
        origin_session_key="telegram:chat-42",
        ),
    )


def test_private_source_generates_opaque_ref_and_reuses_existing_route() -> None:
    resolver = SourceContextResolver(source_ref_factory=lambda: "src_deadbeef")
    context = private_context()
    control_plane_route_id = UUID("00000000-0000-0000-0000-000000000070")

    first = resolver.resolve(context, {}, existing_routes={})
    second = resolver.resolve(
        context,
        {},
        existing_routes={
            "src_existing": PrivateRoute(
                channel="telegram",
                chat_id="chat-42",
                session_key="telegram:chat-42",
                control_plane_route_id=control_plane_route_id,
            )
        },
    )

    assert first.source_type == "akashic_private"
    assert first.source_ref == "src_deadbeef"
    assert first.private_route is not None
    assert second.source_ref == "src_existing"
    assert second.private_route is not None
    assert second.private_route.channel == first.private_route.channel
    assert second.private_route.chat_id == first.private_route.chat_id
    assert second.private_route.session_key == first.private_route.session_key
    assert second.private_route.control_plane_route_id == control_plane_route_id
    assert "telegram" not in first.source_ref
    assert "chat-42" not in first.source_ref


def test_web_source_is_opaque_and_has_no_private_route() -> None:
    resolver = SourceContextResolver(source_ref_factory=lambda: "src_webref")

    result = resolver.resolve(
        cast(
            ToolExecutionContext,
            SimpleNamespace(
                origin_channel="",
                origin_chat_id="",
                origin_session_key="web:thread-1",
            ),
        ),
        {"source": "sidera_web"},
        existing_routes={},
    )

    assert result.source_type == "sidera_web_chat"
    assert result.source_ref == "src_webref"
    assert result.private_route is None


@pytest.mark.parametrize(
    "field",
    ["origin_channel", "origin_chat_id", "origin_session_key"],
)
def test_missing_private_provenance_fails_loud(field: str) -> None:
    context = private_context()
    setattr(context, field, "")

    with pytest.raises(MissingToolContext):
        SourceContextResolver().resolve(context, {}, existing_routes={})
