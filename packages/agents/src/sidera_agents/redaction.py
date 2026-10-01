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
