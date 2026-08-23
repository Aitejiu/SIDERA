"""Stable error categories exposed by the provider-neutral Gateway layer."""


class GatewayError(Exception):
    """Base class for errors that can be mapped to the Gateway API."""


class ProviderError(GatewayError):
    """Base class for failures at the configured main-agent provider."""


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
