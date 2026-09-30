"""Stable error categories exposed by the provider-neutral Gateway layer."""


class GatewayError(Exception):
    """Base class for errors that can be mapped to the Gateway API."""


class AdapterConfigurationUnavailable(GatewayError):
    """The selected adapter is not configured, registered, or installed."""

    _CODES = frozenset(
        {
            "main_agent_not_configured",
            "adapter_not_installed",
            "adapter_not_registered",
            "adapter_registration_mismatch",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ValueError("unsupported adapter unavailability code")
        self.code = code
        super().__init__("main agent adapter is unavailable")


class ProviderError(GatewayError):
    """Base class for failures at the configured main-agent provider."""


class AcceptanceGuardRejected(GatewayError):
    """A bounded acceptance operation was rejected before provider I/O."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProviderUnavailable(ProviderError):
    """The provider could not be reached or is temporarily unavailable."""


class ProviderProtocolError(ProviderError):
    """The provider returned a response outside the adapter contract."""


class ThreadNotFound(GatewayError):
    """The requested thread does not exist in the configured scope."""


class ThreadAccessDenied(GatewayError):
    """The requested thread exists but is outside the configured web scope."""


class ThreadBusy(GatewayError):
    """The thread already has an active turn."""


class TurnNotFound(GatewayError):
    """The requested turn does not exist in the requested thread."""


class TurnStreamUnavailable(GatewayError):
    """The provider turn exists but its live event stream is unavailable."""
