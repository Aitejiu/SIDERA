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
    _STORE_NAME = re.compile(r"^store:[A-Za-z0-9_.-]+$")

    def resolve(self, reference: str) -> SecretStr:
        if not reference.strip():
            raise SecretResolutionError("invalid_reference", reference)
        if reference.startswith("store:"):
            if self._STORE_NAME.fullmatch(reference) is None:
                raise SecretResolutionError("invalid_reference", reference)
            if os.environ.get("SIDERA_CREDENTIAL_STORE_READY") != "1":
                raise SecretResolutionError(
                    "credential_store_unavailable", reference
                )
            # OpenCode resolves the provider credential from its own local store.
            # SIDERA only carries a non-sensitive marker through the adapter API.
            return SecretStr(f"credential-store:{reference.removeprefix('store:')}")
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
