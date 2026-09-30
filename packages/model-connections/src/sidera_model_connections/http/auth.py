import hmac
import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr
from starlette.requests import Request

from sidera_model_connections.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    id: str
    workspace_ids: frozenset[UUID]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not isinstance(self.workspace_ids, frozenset):
            raise TypeError("invalid management principal")
        if any(not isinstance(workspace_id, UUID) for workspace_id in self.workspace_ids):
            raise TypeError("invalid management principal")


@dataclass(frozen=True, slots=True)
class LoopbackManagementAuthorizer:
    token: SecretStr = field(repr=False)
    allowed_origin: str
    principal: ManagementPrincipal

    def __post_init__(self) -> None:
        if not isinstance(self.token, SecretStr) or not self.token.get_secret_value():
            raise ValueError("invalid management authorizer configuration")
        if (
            not isinstance(self.allowed_origin, str)
            or not self.allowed_origin
            or self.allowed_origin == "null"
        ):
            raise ValueError("invalid management authorizer configuration")
        if not isinstance(self.principal, ManagementPrincipal):
            raise TypeError("invalid management authorizer configuration")

    def __call__(self, request: Request) -> ManagementPrincipal:
        if not _is_loopback_host(request.headers.get("host")):
            raise ConfigurationError("management_unauthorized") from None

        origin = request.headers.get("origin")
        if origin is not None and origin != self.allowed_origin:
            raise ConfigurationError("management_unauthorized") from None

        authorization = request.headers.get("authorization", "")
        scheme, separator, presented = authorization.partition(" ")
        matches = hmac.compare_digest(
            presented.encode("utf-8"),
            self.token.get_secret_value().encode("utf-8"),
        )
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not presented
            or any(character.isspace() for character in presented)
            or not matches
        ):
            raise ConfigurationError("management_unauthorized") from None
        return self.principal


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    try:
        parsed = urlsplit(f"//{host}")
        if (
            parsed.netloc != host
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        _ = parsed.port
        hostname = parsed.hostname
        return hostname is not None and ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
