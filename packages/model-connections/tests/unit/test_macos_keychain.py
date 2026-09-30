from __future__ import annotations

import importlib
import json
import subprocess
from uuid import UUID

import pytest
from pydantic import SecretStr

from sidera_model_connections.errors import ConfigurationError
from tests.fakes import fake_ref

NAMESPACE = UUID(int=41)
FAKE_SECRET = "cfg2-fake-main-key"


def adapter_module():
    try:
        return importlib.import_module("sidera_model_connections.macos_keychain")
    except ModuleNotFoundError as exc:
        if exc.name == "sidera_model_connections.macos_keychain":
            pytest.fail("macOS Keychain adapter module is not implemented yet")
        raise


class ControlledProcess:
    def __init__(self, owner: FakePopen) -> None:
        self.owner = owner
        self.communicate_calls = 0
        self.kill_calls = 0
        self.returncode = owner.returncode

    def communicate(self, input: str | None = None, timeout: float | None = None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            self.owner.stdin = input
            self.owner.timeout_value = timeout
            if self.owner.communication_error:
                raise OSError(f"pipe failed for {FAKE_SECRET}")
            if self.owner.timeout:
                raise subprocess.TimeoutExpired(self.owner.argv, timeout)
            return self.owner.stdout, None
        return "", None

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class FakePopen:
    def __init__(self) -> None:
        self.timeout = False
        self.communication_error = False
        self.stdout = json.dumps({"ok": True})
        self.returncode = 0
        self.invocations = 0
        self.argv: list[str] = []
        self.kwargs: dict[str, object] = {}
        self.stdin: str | None = None
        self.timeout_value: float | None = None
        self.process: ControlledProcess | None = None

    def __call__(self, argv, **kwargs) -> ControlledProcess:
        self.invocations += 1
        self.argv = list(argv)
        self.kwargs = kwargs
        self.process = ControlledProcess(self)
        return self.process

    @property
    def secret_was_in_stdin_only(self) -> bool:
        if self.stdin is None:
            return False
        return (
            FAKE_SECRET in self.stdin
            and FAKE_SECRET not in repr(self.argv)
            and FAKE_SECRET not in repr(self.kwargs.get("env"))
        )


def make_fake_helper(monkeypatch) -> FakePopen:
    fake = FakePopen()
    monkeypatch.setattr(adapter_module(), "Popen", fake)
    return fake


def test_put_new_starts_isolated_module_and_keeps_secret_in_stdin_only(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert fake_helper.invocations == 1
    assert fake_helper.argv[-3:] == [
        "-I",
        "-m",
        "sidera_model_connections.keychain_helper",
    ]
    assert fake_helper.kwargs["stdin"] == subprocess.PIPE
    assert fake_helper.kwargs["stdout"] == subprocess.PIPE
    assert fake_helper.kwargs["stderr"] == subprocess.DEVNULL
    assert fake_helper.secret_was_in_stdin_only
    assert set(fake_helper.kwargs["env"]) <= {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
    }


def test_read_returns_secret_from_helper_pipe(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.stdout = json.dumps({"ok": True, "secret": FAKE_SECRET})

    assert store.read(fake_ref()).get_secret_value() == FAKE_SECRET
    assert fake_helper.invocations == 1
    assert FAKE_SECRET not in fake_helper.argv.__repr__()


def test_child_error_details_are_not_exposed(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.stdout = json.dumps(
        {"ok": False, "code": "credential_store_locked", "details": FAKE_SECRET}
    )
    fake_helper.returncode = 1

    with pytest.raises(ConfigurationError) as rejected:
        store.read(fake_ref())

    assert rejected.value.code == "credential_store_locked"
    assert FAKE_SECRET not in str(rejected.value)


def test_timeout_does_not_retry_or_echo_secret(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.timeout = True

    with pytest.raises(ConfigurationError) as rejected:
        store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert rejected.value.code == "credential_write_unknown"
    assert fake_helper.invocations == 1
    assert FAKE_SECRET not in str(rejected.value)
    assert fake_helper.secret_was_in_stdin_only
    assert fake_helper.timeout_value == 10
    assert fake_helper.process is not None
    assert fake_helper.process.kill_calls == 1
    assert fake_helper.process.communicate_calls == 2


def test_duplicate_helper_result_is_a_fixed_conflict(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.stdout = json.dumps({"ok": False, "code": "credential_already_exists"})
    fake_helper.returncode = 1

    with pytest.raises(ConfigurationError) as rejected:
        store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert rejected.value.code == "credential_already_exists"
    assert fake_helper.invocations == 1


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [("", 9), ("not-json", 1), (json.dumps({"ok": True}), 9)],
)
def test_put_without_consistent_terminal_ack_is_unknown(
    monkeypatch,
    stdout: str,
    returncode: int,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.stdout = stdout
    fake_helper.returncode = returncode

    with pytest.raises(ConfigurationError) as rejected:
        store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert rejected.value.code == "credential_write_unknown"
    assert fake_helper.invocations == 1
    assert FAKE_SECRET not in str(rejected.value)
    assert fake_helper.secret_was_in_stdin_only


def test_put_communication_error_after_launch_is_unknown_and_reaped(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.communication_error = True

    with pytest.raises(ConfigurationError) as rejected:
        store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert rejected.value.code == "credential_write_unknown"
    assert FAKE_SECRET not in str(rejected.value)
    assert fake_helper.invocations == 1
    assert fake_helper.secret_was_in_stdin_only
    assert fake_helper.process is not None
    assert fake_helper.process.kill_calls == 1
    assert fake_helper.process.communicate_calls == 2


def test_read_communication_error_is_unavailable_and_reaped(monkeypatch) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.communication_error = True

    with pytest.raises(ConfigurationError) as rejected:
        store.read(fake_ref())

    assert rejected.value.code == "credential_store_unavailable"
    assert fake_helper.invocations == 1
    assert fake_helper.process is not None
    assert fake_helper.process.kill_calls == 1
    assert fake_helper.process.communicate_calls == 2


def test_put_process_start_failure_remains_known_unavailable(monkeypatch) -> None:
    module = adapter_module()

    def fail_before_launch(argv, **kwargs):
        raise OSError(f"could not start helper {FAKE_SECRET}")

    monkeypatch.setattr(module, "Popen", fail_before_launch)
    store = module.MacOSKeychainSecretStore(NAMESPACE)

    with pytest.raises(ConfigurationError) as rejected:
        store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert rejected.value.code == "credential_store_unavailable"
    assert FAKE_SECRET not in str(rejected.value)


def test_parent_preserves_helper_reported_unknown_write(
    monkeypatch,
) -> None:
    fake_helper = make_fake_helper(monkeypatch)
    store = adapter_module().MacOSKeychainSecretStore(NAMESPACE)
    fake_helper.stdout = json.dumps(
        {"ok": False, "code": "credential_write_unknown"}
    )
    fake_helper.returncode = 1

    with pytest.raises(ConfigurationError) as rejected:
        store.put_new(fake_ref(), SecretStr(FAKE_SECRET))

    assert rejected.value.code == "credential_write_unknown"
    assert fake_helper.invocations == 1
    assert FAKE_SECRET not in str(rejected.value)
