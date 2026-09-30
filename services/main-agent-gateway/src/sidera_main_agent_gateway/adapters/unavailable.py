from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NoReturn

from sidera_main_agent_gateway.contracts import (
    JsonValue,
    MainAgentAdapter,
    MainAgentThread,
    MainAgentThreadPage,
    MainAgentTurn,
    MainAgentTurnHandle,
)
from sidera_main_agent_gateway.errors import AdapterConfigurationUnavailable


class UnavailableMainAgentAdapter(MainAgentAdapter):
    def __init__(self, code: str) -> None:
        self._code = AdapterConfigurationUnavailable(code).code

    def _raise_unavailable(self) -> NoReturn:
        raise AdapterConfigurationUnavailable(self._code)

    async def health(self) -> Mapping[str, JsonValue]:
        self._raise_unavailable()

    async def capabilities(self) -> Mapping[str, JsonValue]:
        return {"reason": self._code}

    async def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MainAgentThreadPage:
        self._raise_unavailable()

    async def create_thread(
        self,
        *,
        title: str,
        context_refs: Sequence[str],
    ) -> MainAgentThread:
        self._raise_unavailable()

    async def read_thread(self, thread_id: str) -> MainAgentThread:
        self._raise_unavailable()

    async def start_turn(
        self,
        thread_id: str,
        input_text: str,
        *,
        operation_id: str | None = None,
    ) -> MainAgentTurnHandle:
        self._raise_unavailable()

    async def read_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        self._raise_unavailable()

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> MainAgentTurn:
        self._raise_unavailable()

    async def close(self) -> None:
        return None
