from typing import Literal

ConfigurationErrorCode = Literal[
    "invalid_configuration",
    "configuration_not_found",
    "configuration_version_conflict",
    "credential_not_found",
    "credential_revoked",
    "credential_unresolved",
    "credential_store_unavailable",
    "credential_store_locked",
    "credential_write_unknown",
    "credential_operation_conflict",
    "credential_already_exists",
    "runtime_binding_denied",
    "connection_role_denied",
    "connection_target_denied",
    "management_unauthorized",
    "management_scope_denied",
]

CONFIGURATION_ERROR_CODES = frozenset(ConfigurationErrorCode.__args__)


class ConfigurationError(Exception):
    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in CONFIGURATION_ERROR_CODES:
            raise ValueError("unsupported configuration error code")
        self.code = code
        super().__init__(code)
