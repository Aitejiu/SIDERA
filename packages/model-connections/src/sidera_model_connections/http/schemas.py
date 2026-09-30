from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from sidera_model_connections.contracts import (
    ConnectionAvailability,
    CredentialSource,
    CredentialStatus,
    CredentialWriteStatus,
    NetworkScope,
    ProtocolName,
    Role,
)


class InputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectionInput(InputSchema):
    name: str = Field(min_length=1, max_length=100)
    provider_id: str
    protocol: ProtocolName
    base_url: str
    network_scope: NetworkScope
    allowed_roles: frozenset[Role]
    credential_id: UUID


class ConnectionRevisionInput(ConnectionInput):
    expected_revision: int = Field(ge=1, strict=True)


class CredentialWriteInput(InputSchema):
    operation_id: UUID
    alias: str = Field(min_length=1, max_length=100)
    secret: SecretStr
    replaces_id: UUID | None = None


class EnvCredentialInput(InputSchema):
    alias: str = Field(min_length=1, max_length=100)
    env_name: str


class CredentialRevokeInput(InputSchema):
    expected_version: int = Field(ge=1, strict=True)


class ConnectionSummarySchema(OutputSchema):
    workspace_id: UUID
    connection_id: UUID
    revision: int
    name: str
    provider_id: str
    protocol: ProtocolName
    base_url: str
    network_scope: NetworkScope
    allowed_roles: frozenset[Role]
    credential_id: UUID
    availability: ConnectionAvailability


class CredentialSummarySchema(OutputSchema):
    workspace_id: UUID
    credential_id: UUID
    alias: str
    source: CredentialSource
    status: CredentialStatus
    version: int
    replaces_id: UUID | None


class CredentialWriteSummarySchema(OutputSchema):
    workspace_id: UUID
    operation_id: UUID
    status: CredentialWriteStatus
    credential: CredentialSummarySchema


class CredentialRevocationSchema(OutputSchema):
    credential: CredentialSummarySchema
    cleanup_pending: bool
