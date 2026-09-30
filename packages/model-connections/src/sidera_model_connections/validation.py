import ipaddress
import re
import unicodedata
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import SplitResult, urlsplit, urlunsplit

from sidera_model_connections.contracts import NetworkScope
from sidera_model_connections.errors import ConfigurationError

_LOOPBACKS = frozenset({"127.0.0.1", "::1"})
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _invalid() -> ConfigurationError:
    return ConfigurationError("invalid_configuration")


def _parse_endpoint(
    base_url: str,
) -> tuple[
    SplitResult,
    str,
    int | None,
    str,
    str,
    IPv4Address | IPv6Address | None,
]:
    if not isinstance(base_url, str) or not base_url:
        raise _invalid()
    if any(
        character == "\\"
        or character.isspace()
        or unicodedata.category(character) == "Cc"
        for character in base_url
    ):
        raise _invalid()
    try:
        parsed = urlsplit(base_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _invalid() from None

    if scheme not in {"http", "https"} or not parsed.netloc or not host:
        raise _invalid()
    if "@" in parsed.netloc or "?" in base_url or "#" in base_url:
        raise _invalid()
    if parsed.query or parsed.fragment or parsed.netloc.endswith(":"):
        raise _invalid()
    if port is not None and not 1 <= port <= 65535:
        raise _invalid()
    if "%" in host:
        raise _invalid()

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None:
        if isinstance(address, ipaddress.IPv6Address):
            normalized_host = f"[{address.compressed}]"
        else:
            normalized_host = address.compressed
        canonical_host = address.compressed
    else:
        if host.endswith(".."):
            raise _invalid()
        try:
            canonical_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise _invalid() from None
        labels = canonical_host.split(".")
        if not canonical_host or len(canonical_host) > 253 or any(
            not _HOST_LABEL.fullmatch(label) for label in labels
        ):
            raise _invalid()
        normalized_host = canonical_host

    return parsed, scheme, port, normalized_host, canonical_host, address


def validate_endpoint(base_url: str, network_scope: NetworkScope) -> str:
    parsed, scheme, port, normalized_host, canonical_host, address = _parse_endpoint(
        base_url
    )
    if network_scope == "public":
        if scheme != "https":
            raise _invalid()
        if canonical_host == "localhost" or canonical_host.endswith(".localhost"):
            raise _invalid()
        if address is not None and (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise _invalid()
    elif network_scope == "loopback":
        if address is None or canonical_host not in _LOOPBACKS:
            raise _invalid()
    else:
        raise _invalid()

    default_port = 443 if scheme == "https" else 80
    authority = normalized_host
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return urlunsplit((scheme, authority, parsed.path, "", ""))


def endpoint_origin(base_url: str) -> str:
    _parsed, scheme, port, normalized_host, _, _ = _parse_endpoint(base_url)
    default_port = 443 if scheme == "https" else 80
    authority = normalized_host
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return urlunsplit((scheme, authority, "", "", ""))
