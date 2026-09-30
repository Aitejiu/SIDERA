from __future__ import annotations

import importlib
import io
import json
from uuid import UUID

import pytest

from sidera_model_connections.errors import ConfigurationError

NAMESPACE = UUID(int=31)
WORKSPACE_ID = UUID(int=32)
CREDENTIAL_ID = UUID(int=33)
SERVICE = f"org.sidera.credentials.{NAMESPACE}"
ACCOUNT = f"{WORKSPACE_ID}:{CREDENTIAL_ID}"
FAKE_SECRET = "cfg2-fake-main-key"


def helper_module():
    try:
        return importlib.import_module("sidera_model_connections.keychain_helper")
    except ModuleNotFoundError as exc:
        if exc.name == "sidera_model_connections.keychain_helper":
            pytest.fail("keychain helper module is not implemented yet")
        raise


class FakeSecurity:
    kSecClass = "class"
    kSecClassGenericPassword = "generic-password"
    kSecAttrService = "service"
    kSecAttrAccount = "account"
    kSecValueData = "value-data"
    kSecReturnData = "return-data"
    kSecMatchLimit = "match-limit"
    kSecMatchLimitOne = "limit-one"
    kSecUseAuthenticationUI = "authentication-ui"
    kSecUseAuthenticationUIFail = "authentication-ui-fail"
    errSecSuccess = 0
    errSecDuplicateItem = -25299
    errSecItemNotFound = -25300
    errSecInteractionNotAllowed = -25308
    errSecAuthFailed = -25293
    errSecInteractionRequired = -25315
    errSecUserCanceled = -128

    def __init__(self) -> None:
        self.add_status = self.errSecSuccess
        self.copy_status = self.errSecSuccess
        self.delete_status = self.errSecSuccess
        self.copy_data = FAKE_SECRET.encode("utf-8")
        self.calls: list[tuple[str, dict[object, object]]] = []

    def SecItemAdd(self, query: dict[object, object], result: object) -> int:
        assert result is None
        self.calls.append(("add", query))
        return self.add_status

    def SecItemCopyMatching(
        self,
        query: dict[object, object],
        result: object,
    ) -> tuple[int, bytes]:
        assert result is None
        self.calls.append(("copy", query))
        return self.copy_status, self.copy_data

    def SecItemDelete(self, query: dict[object, object]) -> int:
        self.calls.append(("delete", query))
        return self.delete_status


def request(operation: str, *, secret: str | None = None) -> dict[str, str]:
    payload = {
        "operation": operation,
        "namespace": str(NAMESPACE),
        "workspace_id": str(WORKSPACE_ID),
        "credential_id": str(CREDENTIAL_ID),
    }
    if secret is not None:
        payload["secret"] = secret
    return payload


def test_put_new_adds_exact_item_and_never_overwrites_duplicate() -> None:
    security = FakeSecurity()
    security.add_status = security.errSecDuplicateItem

    with pytest.raises(ConfigurationError) as rejected:
        helper_module()._execute(request("put_new", secret=FAKE_SECRET), security)

    assert rejected.value.code == "credential_already_exists"
    assert security.calls == [
        (
            "add",
            {
                security.kSecClass: security.kSecClassGenericPassword,
                security.kSecAttrService: SERVICE,
                security.kSecAttrAccount: ACCOUNT,
                security.kSecValueData: FAKE_SECRET.encode("utf-8"),
                security.kSecUseAuthenticationUI: security.kSecUseAuthenticationUIFail,
            },
        )
    ]


def test_read_matches_one_exact_item_and_returns_only_its_data() -> None:
    security = FakeSecurity()

    value = helper_module()._execute(request("read"), security)

    assert value == FAKE_SECRET
    assert security.calls == [
        (
            "copy",
            {
                security.kSecClass: security.kSecClassGenericPassword,
                security.kSecAttrService: SERVICE,
                security.kSecAttrAccount: ACCOUNT,
                security.kSecReturnData: True,
                security.kSecMatchLimit: security.kSecMatchLimitOne,
                security.kSecUseAuthenticationUI: security.kSecUseAuthenticationUIFail,
            },
        )
    ]


def test_read_missing_item_is_unresolved_without_os_details() -> None:
    security = FakeSecurity()
    security.copy_status = security.errSecItemNotFound

    with pytest.raises(ConfigurationError) as rejected:
        helper_module()._execute(request("read"), security)

    assert rejected.value.code == "credential_unresolved"
    assert "-25300" not in str(rejected.value)


def test_delete_targets_exact_item_and_refuses_authentication_ui() -> None:
    security = FakeSecurity()

    assert helper_module()._execute(request("delete_owned"), security) is None
    assert security.calls == [
        (
            "delete",
            {
                security.kSecClass: security.kSecClassGenericPassword,
                security.kSecAttrService: SERVICE,
                security.kSecAttrAccount: ACCOUNT,
                security.kSecUseAuthenticationUI: security.kSecUseAuthenticationUIFail,
            },
        )
    ]


@pytest.mark.parametrize(
    "status_name",
    ["errSecInteractionNotAllowed", "errSecAuthFailed", "errSecInteractionRequired"],
)
def test_locked_or_denied_access_uses_fixed_error_and_no_ui(
    status_name: str,
) -> None:
    security = FakeSecurity()
    security.copy_status = getattr(security, status_name)

    with pytest.raises(ConfigurationError) as rejected:
        helper_module()._execute(request("read"), security)

    assert rejected.value.code == "credential_store_locked"
    assert FAKE_SECRET not in str(rejected.value)
    assert security.calls[0][1][security.kSecUseAuthenticationUI] == (
        security.kSecUseAuthenticationUIFail
    )


def test_missing_ui_fail_constant_refuses_operation_as_unavailable() -> None:
    security = FakeSecurity()
    security.kSecUseAuthenticationUIFail = None

    with pytest.raises(ConfigurationError) as rejected:
        helper_module()._execute(request("read"), security)

    assert rejected.value.code == "credential_store_unavailable"
    assert security.calls == []


def test_helper_rejects_arbitrary_item_identity_fields() -> None:
    security = FakeSecurity()
    payload = request("read") | {"service": "some.other.service", "account": "other"}

    with pytest.raises(ConfigurationError) as rejected:
        helper_module()._execute(payload, security)

    assert rejected.value.code == "invalid_configuration"
    assert security.calls == []


def test_helper_protocol_serializes_only_fixed_error_codes(monkeypatch) -> None:
    class BrokenSecurity(FakeSecurity):
        def SecItemCopyMatching(self, query: dict, result: object) -> tuple[int, bytes]:
            self.calls.append(("copy", query))
            raise RuntimeError(f"private os detail {FAKE_SECRET}")

    module = helper_module()
    monkeypatch.setattr(module, "_load_security", lambda: BrokenSecurity())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request("read"))))
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)

    exit_code = module.main()
    response = json.loads(output.getvalue())

    assert exit_code != 0
    assert response == {"ok": False, "code": "credential_store_unavailable"}
    assert FAKE_SECRET not in output.getvalue()
    assert "private os detail" not in output.getvalue()


def test_missing_security_framework_is_unavailable_without_details(monkeypatch) -> None:
    def unavailable() -> object:
        raise ImportError("Security is not installed")

    module = helper_module()
    monkeypatch.setattr(module, "_load_security", unavailable)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request("read"))))
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)

    exit_code = module.main()
    response = json.loads(output.getvalue())

    assert exit_code != 0
    assert response == {"ok": False, "code": "credential_store_unavailable"}
    assert "Security is not installed" not in output.getvalue()


def test_add_exception_after_recording_item_projects_unknown_write(monkeypatch) -> None:
    class WritesThenRaises(FakeSecurity):
        stored_data: bytes | None = None

        def SecItemAdd(self, query: dict, result: object) -> int:
            assert result is None
            self.calls.append(("add", query))
            self.stored_data = query[self.kSecValueData]
            raise RuntimeError(f"bridge failed after add {FAKE_SECRET}")

    security = WritesThenRaises()
    module = helper_module()
    monkeypatch.setattr(module, "_load_security", lambda: security)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps(request("put_new", secret=FAKE_SECRET))),
    )
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)

    exit_code = module.main()
    response = json.loads(output.getvalue())

    assert security.stored_data == FAKE_SECRET.encode("utf-8")
    assert exit_code != 0
    assert response == {"ok": False, "code": "credential_write_unknown"}
    assert FAKE_SECRET not in output.getvalue()
    assert "bridge failed after add" not in output.getvalue()
