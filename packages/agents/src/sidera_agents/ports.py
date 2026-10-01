from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from .contracts import SecretBinding, SourceContent, SourceRef, ToolConnection
from .errors import AgentError


class ExecutionAuthorizer(Protocol):
    def require(self, grant_id: UUID, action: str, target: object) -> None: ...


class ContextSourceReader(Protocol):
    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent: ...


class SecretReader(Protocol):
    def read(self, grant_id: UUID, binding: SecretBinding) -> SecretStr: ...


class ToolConnectionReader(Protocol):
    def resolve(self, grant_id: UUID, connection_id: UUID) -> ToolConnection: ...


def _deny() -> None:
    raise AgentError("permission_denied", "Access denied.")


class DenyExecutionAuthorizer:
    def require(self, grant_id: UUID, action: str, target: object) -> None:
        _deny()


class DenyContextSourceReader:
    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        _deny()


class DenySecretReader:
    def read(self, grant_id: UUID, binding: SecretBinding) -> SecretStr:
        _deny()


class DenyToolConnectionReader:
    def resolve(self, grant_id: UUID, connection_id: UUID) -> ToolConnection:
        _deny()
