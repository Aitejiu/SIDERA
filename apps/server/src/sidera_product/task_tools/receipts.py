import hashlib
import json
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import anyio
from pydantic import JsonValue, SecretStr, TypeAdapter, ValidationError
from sidera_agents.errors import AgentError

from sidera_product.auth.grants import ProductGrants, VerifiedToolGrant
from sidera_product.postgres.authority_store import (
    GrantRole,
    ReceiptStore,
    ToolReceiptRecord,
)

from .transport import MutationResult, OriginalCommandTransport

_MUTATIONS: dict[GrantRole, frozenset[str]] = {
    "main": frozenset({"sidera_submit_goal", "sidera_request_revision"}),
    "worker": frozenset({"sidera_report_progress"}),
    "planner": frozenset(),
}
_JSON = TypeAdapter(dict[str, JsonValue])
type PrepareCommand = Callable[
    [VerifiedToolGrant, UUID], Awaitable[dict[str, JsonValue]]
]


def _conflict() -> None:
    raise AgentError(
        "tool_intent_conflict", "The original logical tool intent cannot change"
    )


def payload_hash(payload: dict[str, JsonValue]) -> str:
    try:
        data = _JSON.validate_python(payload)
        encoded = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (ValueError, TypeError):
        raise AgentError(
            "invalid_tool_arguments", "Tool arguments must be finite JSON"
        ) from None
    return hashlib.sha256(encoded).hexdigest()


class ToolReceipts:
    def __init__(self, grants: ProductGrants, store: ReceiptStore) -> None:
        self.grants, self.store = grants, store

    async def _authorized(self, token: SecretStr, tool_name: str) -> VerifiedToolGrant:
        verified = await anyio.to_thread.run_sync(
            lambda: self.grants.verify(token, require_running=True)
        )
        if tool_name not in _MUTATIONS[verified.record.role]:
            raise AgentError(
                "permission_denied", "The current Run cannot use this tool"
            )
        return verified

    @staticmethod
    def _same_intent(
        row: ToolReceiptRecord, verified: VerifiedToolGrant, digest: str
    ) -> None:
        if (
            row.grant_id != verified.record.grant_id
            or row.run_id != verified.run.run_id
            or row.response_id != verified.run.active_response_id
            or row.payload_hash != digest
        ):
            _conflict()

    @staticmethod
    def _result(row: ToolReceiptRecord) -> MutationResult:
        if row.result is not None:
            try:
                result = MutationResult.model_validate(row.result)
                if (
                    result.command_id == row.command_id
                    and result.outcome == row.outcome
                ):
                    return result
            except ValidationError:
                pass
        return MutationResult(
            outcome="unknown",
            command_id=row.command_id,
            status="unknown",
            error_code="command_result_unknown",
        )

    async def execute(
        self,
        token: SecretStr,
        tool_name: str,
        logical_request_id: UUID,
        payload: dict[str, JsonValue],
        *,
        prepare: PrepareCommand,
        transport: OriginalCommandTransport,
    ) -> MutationResult:
        verified = await self._authorized(token, tool_name)
        if type(logical_request_id) is not UUID:
            raise AgentError(
                "invalid_tool_arguments", "A logical request UUID is required"
            )
        digest = payload_hash(payload)
        row = await anyio.to_thread.run_sync(
            lambda: self.store.find_receipt(
                grant_id=verified.record.grant_id,
                tool_name=tool_name,
                logical_request_id=logical_request_id,
            )
        )
        winner = False
        if row is None:
            command_id = uuid4()
            intent = _JSON.validate_python(await prepare(verified, command_id))
            if (
                intent.get("command_id") != str(command_id)
                or verified.run.active_response_id is None
            ):
                raise AgentError(
                    "invalid_tool_intent",
                    "A tool command must retain its original identity",
                )
            fresh = await self._authorized(token, tool_name)
            if (
                fresh.run.run_id != verified.run.run_id
                or fresh.run.active_response_id != verified.run.active_response_id
            ):
                _conflict()
            now = self.grants.clock()
            candidate = ToolReceiptRecord(
                uuid4(),
                verified.record.grant_id,
                verified.run.run_id,
                verified.run.active_response_id,
                tool_name,
                logical_request_id,
                digest,
                command_id,
                intent,
                "dispatching",
                None,
                now,
                now,
            )
            row, winner = await anyio.to_thread.run_sync(
                self.store.save_receipt, candidate
            )
        self._same_intent(row, verified, digest)
        if row.outcome not in {"applied", "rejected"}:
            current = await self._authorized(token, tool_name)
            self._same_intent(row, current, digest)
            result = (
                await transport.dispatch(row.intent, row.command_id)
                if winner
                else await transport.reconcile(row.command_id)
            )
            current = await self._authorized(token, tool_name)
            self._same_intent(row, current, digest)
            receipt_id = row.receipt_id
            row = await anyio.to_thread.run_sync(
                lambda: self.store.settle_receipt(
                    receipt_id,
                    outcome=result.outcome,
                    result=_JSON.validate_python(result.model_dump(mode="json")),
                    now=self.grants.clock(),
                )
            )
        current = await self._authorized(token, tool_name)
        self._same_intent(row, current, digest)
        return self._result(row)
