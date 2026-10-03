import re
from collections.abc import Iterable

from pydantic import SecretStr


def redact_secrets(value: str, secrets: Iterable[SecretStr]) -> str:
    values = sorted(
        (secret.get_secret_value() for secret in secrets if secret.get_secret_value()),
        key=len,
        reverse=True,
    )
    for secret in values:
        value = value.replace(secret, "[REDACTED]")
    return value


_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie|password|"
    r"api[_-]?key|access[_-]?token|credential|grant[_-]?id|headers|environment)\s*[=:]"
    r"|\b(?:postgres(?:ql)?(?:\+\w+)?|mysql|mongodb)://"
    r"|[a-z][a-z0-9+.-]*://[^\s/]*@"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


class ObservationRedactor:
    """Run-local exact-value context. Never serialize or pin resolved credentials."""

    def __init__(self, secrets: Iterable[SecretStr]) -> None:
        self._secrets = tuple(secrets)

    def clear(self) -> None:
        self._secrets = ()

    def label(self, value: object, pattern: str, maximum: int = 200) -> str | None:
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or re.fullmatch(pattern, value) is None
            or redact_secrets(value, self._secrets) != value
            or _CREDENTIAL_TEXT.search(value)
        ):
            return None
        return value

    def text(self, value: object, *, maximum_bytes: int = 8192) -> dict[str, object]:
        if not isinstance(value, str):
            return {"reason_code": "native_content_omitted"}
        value = redact_secrets(value, self._secrets)
        if _CREDENTIAL_TEXT.search(value):
            return {"reason_code": "unsafe_text_omitted"}
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return {"reason_code": "unsafe_text_omitted"}
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            return {"reason_code": "unsafe_text_omitted"}
        if len(encoded) > maximum_bytes:
            return {
                "text": encoded[:maximum_bytes].decode("utf-8", errors="ignore"),
                "truncated": True,
                "reason_code": "text_truncated",
            }
        return {"text": value}
