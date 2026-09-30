from __future__ import annotations

import importlib.metadata
import os
import platform
import sys
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sidera_model_connections.contracts import CredentialRef
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.macos_keychain import MacOSKeychainSecretStore

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("SIDERA_CFG2_RUN_OWNED_KEYCHAIN") != "1",
    reason="requires explicit owned macOS Keychain smoke opt-in",
)

FAKE_SECRET = "sidera-cfg2-owned-keychain-smoke-fake-value"
DUPLICATE_SECRET = "sidera-cfg2-owned-keychain-duplicate-must-not-replace"
UNAVAILABLE_CODES = {
    "credential_store_locked",
    "credential_store_unavailable",
    "credential_write_unknown",
}


def test_owned_namespace_round_trip_duplicate_and_exact_cleanup() -> None:
    namespace = uuid4()
    ref = CredentialRef(uuid4(), uuid4())
    store = MacOSKeychainSecretStore(namespace)
    gate_error: tuple[str, str] | None = None
    cleanup_error: str | None = None
    cleanup_verified = False
    stage = "put_new"
    may_own_item = False

    try:
        try:
            store.put_new(ref, SecretStr(FAKE_SECRET))
            may_own_item = True
        except ConfigurationError as exc:
            if exc.code == "credential_write_unknown":
                may_own_item = True
            if exc.code not in UNAVAILABLE_CODES:
                raise
            gate_error = (stage, exc.code)

        if gate_error is None:
            stage = "same-ref read"
            assert store.read(ref).get_secret_value() == FAKE_SECRET

            stage = "new helper process read"
            restarted_store = MacOSKeychainSecretStore(namespace)
            assert restarted_store.read(ref).get_secret_value() == FAKE_SECRET

            stage = "duplicate put"
            with pytest.raises(ConfigurationError) as duplicate:
                store.put_new(ref, SecretStr(DUPLICATE_SECRET))
            assert duplicate.value.code == "credential_already_exists"

            stage = "duplicate preservation read"
            assert store.read(ref).get_secret_value() == FAKE_SECRET
    except ConfigurationError as exc:
        if exc.code not in UNAVAILABLE_CODES:
            raise
        gate_error = (stage, exc.code)
    finally:
        if may_own_item:
            try:
                store.delete_owned(ref)
            except ConfigurationError as exc:
                cleanup_error = exc.code
            else:
                try:
                    store.read(ref)
                except ConfigurationError as exc:
                    if exc.code == "credential_unresolved":
                        cleanup_verified = True
                    else:
                        cleanup_error = exc.code
                else:
                    cleanup_error = "item_still_present"

    pyobjc_version = importlib.metadata.version("pyobjc-framework-Security")
    summary = (
        f"namespace={namespace} workspace_id={ref.workspace_id} "
        f"credential_id={ref.credential_id} python={platform.python_version()} "
        f"macOS={platform.mac_ver()[0]} pyobjc-security={pyobjc_version}"
    )
    if gate_error is not None:
        print(
            f"OWNED_KEYCHAIN_SMOKE UNAVAILABLE stage={gate_error[0]} "
            f"code={gate_error[1]} cleanup="
            f"{'verified' if cleanup_verified else cleanup_error or 'not-needed'} {summary}"
        )
        pytest.skip(
            f"UNAVAILABLE stage={gate_error[0]} code={gate_error[1]} "
            f"cleanup={'verified' if cleanup_verified else cleanup_error or 'not-needed'}"
        )

    assert cleanup_error is None
    assert cleanup_verified
    print(
        f"OWNED_KEYCHAIN_SMOKE PASS duplicate_preserved=true "
        f"new_process_read=true cleanup=verified {summary}"
    )
