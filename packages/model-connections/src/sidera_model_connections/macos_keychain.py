from __future__ import annotations

import json
import os
import sys
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
from uuid import UUID

from pydantic import SecretStr

from sidera_model_connections.contracts import CredentialRef
from sidera_model_connections.errors import ConfigurationError

_HELPER_TIMEOUT_SECONDS = 10
_HELPER_ENVIRONMENT = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
_HELPER_MODULE = "sidera_model_connections.keychain_helper"
_CHILD_ERROR_CODES = frozenset(
    {
        "credential_already_exists",
        "credential_store_locked",
        "credential_store_unavailable",
        "credential_unresolved",
        "credential_write_unknown",
        "invalid_configuration",
    }
)


def _helper_environment() -> dict[str, str]:
    return {
        name: os.environ[name]
        for name in _HELPER_ENVIRONMENT
        if name in os.environ
    }


def _exchange_failure_code(operation: str) -> str:
    return (
        "credential_write_unknown"
        if operation == "put_new"
        else "credential_store_unavailable"
    )


def _terminate_and_reap(process: Popen) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        process.communicate()
    except Exception:  # noqa: BLE001
        process.wait()


def _raise_exchange_failure(process: Popen, operation: str) -> None:
    code = _exchange_failure_code(operation)
    try:
        _terminate_and_reap(process)
    except Exception:  # noqa: BLE001
        raise ConfigurationError(code) from None
    raise ConfigurationError(code) from None


class MacOSKeychainSecretStore:
    def __init__(self, namespace: UUID) -> None:
        if not isinstance(namespace, UUID):
            raise ConfigurationError("invalid_configuration")
        self._namespace = namespace

    def put_new(self, ref: CredentialRef, value: SecretStr) -> None:
        if not isinstance(value, SecretStr):
            raise ConfigurationError("invalid_configuration")
        self._invoke("put_new", ref, value.get_secret_value())

    def read(self, ref: CredentialRef) -> SecretStr:
        response = self._invoke("read", ref)
        secret = response.get("secret")
        if not isinstance(secret, str):
            raise ConfigurationError("credential_store_unavailable")
        return SecretStr(secret)

    def delete_owned(self, ref: CredentialRef) -> None:
        self._invoke("delete_owned", ref)

    def _invoke(
        self,
        operation: str,
        ref: CredentialRef,
        secret: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(ref, CredentialRef):
            raise ConfigurationError("invalid_configuration")
        payload: dict[str, str] = {
            "operation": operation,
            "namespace": str(self._namespace),
            "workspace_id": str(ref.workspace_id),
            "credential_id": str(ref.credential_id),
        }
        if operation == "put_new":
            if secret is None:
                raise ConfigurationError("invalid_configuration")
            payload["secret"] = secret
        try:
            request = json.dumps(payload, separators=(",", ":")) + "\n"
        except (TypeError, ValueError, UnicodeError):
            raise ConfigurationError("invalid_configuration") from None

        try:
            process = Popen(
                [sys.executable, "-I", "-m", _HELPER_MODULE],
                stdin=PIPE,
                stdout=PIPE,
                stderr=DEVNULL,
                text=True,
                env=_helper_environment(),
            )
        except Exception:  # noqa: BLE001
            # The request has not been sent if the child could not start.
            raise ConfigurationError("credential_store_unavailable") from None

        try:
            stdout, _ = process.communicate(
                input=request,
                timeout=_HELPER_TIMEOUT_SECONDS,
            )
        except TimeoutExpired:
            _raise_exchange_failure(process, operation)
        except Exception:  # noqa: BLE001
            # Once launched, the helper may have consumed the complete request.
            _raise_exchange_failure(process, operation)

        try:
            response = json.loads(stdout)
        except (TypeError, ValueError):
            raise ConfigurationError(_exchange_failure_code(operation)) from None
        if not isinstance(response, dict):
            raise ConfigurationError(_exchange_failure_code(operation))
        returncode = process.returncode
        if response.get("ok") is False:
            if returncode != 1:
                raise ConfigurationError(_exchange_failure_code(operation))
            code = response.get("code")
            if isinstance(code, str) and code in _CHILD_ERROR_CODES:
                if code == "credential_write_unknown" and operation != "put_new":
                    raise ConfigurationError("credential_store_unavailable")
                raise ConfigurationError(code)
            raise ConfigurationError(_exchange_failure_code(operation))
        if response.get("ok") is not True:
            raise ConfigurationError(_exchange_failure_code(operation))
        if returncode != 0:
            raise ConfigurationError(_exchange_failure_code(operation))
        return response
