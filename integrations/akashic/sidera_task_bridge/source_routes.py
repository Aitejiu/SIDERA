from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from agent.tools.base import ToolExecutionContext

from .state import PrivateRoute


class MissingToolContext(RuntimeError):
    """The plugin cannot prove which source owns a tool call."""


@dataclass(frozen=True, slots=True)
class SourceResolution:
    source_type: Literal["akashic_private", "sidera_web_chat"]
    source_ref: str
    private_route: PrivateRoute | None


class SourceContextResolver:
    """Map host provenance to an opaque control-plane source reference."""

    def __init__(self, source_ref_factory: Callable[[], str] | None = None) -> None:
        self._source_ref_factory = source_ref_factory or (
            lambda: f"src_{secrets.token_hex(16)}"
        )
    def resolve(
        self,
        context: ToolExecutionContext,
        session_metadata: Mapping[str, object],
        *,
        existing_routes: Mapping[str, PrivateRoute],
    ) -> SourceResolution:
        if session_metadata.get("source") == "sidera_web":
            return SourceResolution(
                source_type="sidera_web_chat",
                source_ref=self._source_ref_factory(),
                private_route=None,
            )

        channel = str(getattr(context, "origin_channel", "") or "").strip()
        chat_id = str(getattr(context, "origin_chat_id", "") or "").strip()
        session_key = str(getattr(context, "origin_session_key", "") or "").strip()
        if not channel or not chat_id or not session_key:
            raise MissingToolContext("private source provenance is incomplete")

        for source_ref, route in existing_routes.items():
            if route.session_key == session_key:
                return SourceResolution(
                    source_type="akashic_private",
                    source_ref=source_ref,
                    private_route=route,
                )

        return SourceResolution(
            source_type="akashic_private",
            source_ref=self._source_ref_factory(),
            private_route=PrivateRoute(
                channel=channel,
                chat_id=chat_id,
                session_key=session_key,
            ),
        )
