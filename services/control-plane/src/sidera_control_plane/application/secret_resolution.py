from __future__ import annotations

import os
import re
from typing import Protocol

from pydantic import SecretStr


class SecretResolutionError(Exception):
    def __init__(self, code: str, reference: str) -> None:
        super().__init__(f"{code}: secret reference could not be resolved")
        self.code = code
        self.reference = reference


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> SecretStr:
        raise NotImplementedError


class EnvironmentSecretResolver:
    _NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

    def resolve(self, reference: str) -> SecretStr:
        if not reference.strip():
            raise SecretResolutionError("invalid_reference", reference)
        if not reference.startswith("env:"):
            raise SecretResolutionError("unsupported_scheme", reference)
        name = reference.removeprefix("env:")
        if self._NAME.fullmatch(name) is None:
            raise SecretResolutionError("invalid_reference", reference)
        value = os.environ.get(name)
        if value is None:
            raise SecretResolutionError("missing_secret", reference)
        return SecretStr(value)

    def resolve_many(self, references: tuple[str, ...]) -> tuple[SecretStr, ...]:
        return tuple(self.resolve(reference) for reference in references)


EnvSecretResolver = EnvironmentSecretResolver
