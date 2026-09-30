from __future__ import annotations

import json
import sys
from uuid import UUID

from sidera_model_connections.errors import ConfigurationError

_OPERATIONS = frozenset({"put_new", "read", "delete_owned"})
_LOCKED_STATUSES = (
    "errSecInteractionNotAllowed",
    "errSecAuthFailed",
    "errSecInteractionRequired",
    "errSecUserCanceled",
)


def _unavailable() -> ConfigurationError:
    return ConfigurationError("credential_store_unavailable")


def _constant(security: object, name: str) -> object:
    value = getattr(security, name, None)
    if value is None:
        raise _unavailable()
    return value


def _identity(payload: object) -> tuple[str, str, str, str | None]:
    if not isinstance(payload, dict) or any(not isinstance(k, str) for k in payload):
        raise ConfigurationError("invalid_configuration")
    operation = payload.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ConfigurationError("invalid_configuration")
    fields = {"operation", "namespace", "workspace_id", "credential_id"}
    if operation == "put_new":
        fields.add("secret")
    if payload.keys() != fields:
        raise ConfigurationError("invalid_configuration")
    try:
        namespace = UUID(payload["namespace"])
        workspace_id = UUID(payload["workspace_id"])
        credential_id = UUID(payload["credential_id"])
    except (AttributeError, TypeError, ValueError):
        raise ConfigurationError("invalid_configuration") from None
    secret = payload.get("secret")
    if operation == "put_new" and not isinstance(secret, str):
        raise ConfigurationError("invalid_configuration")
    service = f"org.sidera.credentials.{namespace}"
    account = f"{workspace_id}:{credential_id}"
    return operation, service, account, secret


def _query(security: object, service: str, account: str) -> dict[object, object]:
    ui_key = _constant(security, "kSecUseAuthenticationUI")
    ui_fail = _constant(security, "kSecUseAuthenticationUIFail")
    return {
        _constant(security, "kSecClass"): _constant(
            security, "kSecClassGenericPassword"
        ),
        _constant(security, "kSecAttrService"): service,
        _constant(security, "kSecAttrAccount"): account,
        ui_key: ui_fail,
    }


def _matches_status(security: object, status: object, name: str) -> bool:
    expected = getattr(security, name, None)
    return expected is not None and status == expected


def _check_status(security: object, status: object, operation: str) -> bool:
    if status == _constant(security, "errSecSuccess"):
        return True
    if operation == "put_new" and _matches_status(
        security, status, "errSecDuplicateItem"
    ):
        raise ConfigurationError("credential_already_exists")
    if operation == "read" and _matches_status(
        security, status, "errSecItemNotFound"
    ):
        raise ConfigurationError("credential_unresolved")
    if operation == "delete_owned" and _matches_status(
        security, status, "errSecItemNotFound"
    ):
        return False
    if any(_matches_status(security, status, name) for name in _LOCKED_STATUSES):
        raise ConfigurationError("credential_store_locked")
    raise _unavailable()


def _status_and_value(result: object) -> tuple[object, object | None]:
    if isinstance(result, tuple):
        if len(result) != 2:
            raise _unavailable()
        return result[0], result[1]
    return result, None


def _as_secret(data: object) -> str:
    try:
        raw = data if isinstance(data, bytes) else bytes(data)
        return raw.decode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise _unavailable() from None


def _execute(payload: object, security: object) -> str | None:
    operation, service, account, secret = _identity(payload)
    query = _query(security, service, account)

    try:
        if operation == "put_new":
            try:
                value_data = secret.encode("utf-8")
            except UnicodeError:
                raise ConfigurationError("invalid_configuration") from None
            add_query = {
                **query,
                _constant(security, "kSecValueData"): value_data,
            }
            _constant(security, "errSecSuccess")
            add_item = getattr(security, "SecItemAdd", None)
            if not callable(add_item):
                raise _unavailable()
            try:
                add_result = add_item(add_query, None)
            except Exception:  # noqa: BLE001
                # SecItemAdd may have stored the item before the bridge failed.
                raise ConfigurationError("credential_write_unknown") from None
            try:
                status, _ = _status_and_value(add_result)
            except ConfigurationError:
                raise ConfigurationError("credential_write_unknown") from None
            _check_status(security, status, operation)
            return None

        if operation == "read":
            read_query = {
                **query,
                _constant(security, "kSecReturnData"): True,
                _constant(security, "kSecMatchLimit"): _constant(
                    security, "kSecMatchLimitOne"
                ),
            }
            status, data = _status_and_value(
                security.SecItemCopyMatching(read_query, None)
            )
            _check_status(security, status, operation)
            if data is None:
                raise _unavailable()
            return _as_secret(data)

        status = security.SecItemDelete(query)
        if isinstance(status, tuple):
            status = status[0] if status else None
        _check_status(security, status, operation)
        return None
    except ConfigurationError:
        raise
    except Exception:  # noqa: BLE001
        # Security bridge exceptions can contain item-specific details.
        raise _unavailable() from None


def _load_security() -> object:
    try:
        import Security
    except ImportError:
        raise _unavailable() from None
    return Security


def main() -> int:
    try:
        payload = json.loads(sys.stdin.readline())
        security = _load_security()
        secret = _execute(payload, security)
        response: dict[str, object] = {"ok": True}
        if payload.get("operation") == "read":
            response["secret"] = secret
        exit_code = 0
    except ConfigurationError as exc:
        response = {"ok": False, "code": exc.code}
        exit_code = 1
    except Exception:  # noqa: BLE001
        # The helper protocol must not write exception details to stderr.
        response = {"ok": False, "code": "credential_store_unavailable"}
        exit_code = 1
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
