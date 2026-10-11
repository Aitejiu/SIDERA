import traceback
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sidera_agents.contracts import CredentialRef, SecretBinding
from sidera_agents.errors import AgentError
from sidera_model_connections.contracts import CredentialRef as ModelCredentialRef
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.native_access import NativeCredentialAccessRequest

from sidera_product import composition
from sidera_product.config import ProductSettings
from tests.support.controlled_native import ControlledSecretStore


def test_secret_binding_maps_exact_public_native_request(
    settings_values, no_database_connections, monkeypatch
):
    runtime = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    scope, credential = uuid4(), uuid4()
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=runtime.settings.workspace_id, credential_id=credential
        ),
        destination="PRODUCT_CONTROLLED_API_KEY",
    )
    captured = []
    controlled_value = SecretStr("test-owned-not-a-real-credential")

    def read(request):
        captured.append(request)
        return controlled_value

    monkeypatch.setattr(runtime.credential_reader, "read", read)
    try:
        assert runtime.secret_reader.read(scope, binding) is controlled_value
        assert captured == [
            NativeCredentialAccessRequest(
                workspace_id=runtime.settings.workspace_id,
                credential_ref=ModelCredentialRef(
                    runtime.settings.workspace_id, credential
                ),
                scope_id=scope,
                destination=binding.destination,
            )
        ]
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_native_permission_maps_and_denies_every_changed_scope_field():
    workspace, credential, scope = uuid4(), uuid4(), uuid4()
    request = NativeCredentialAccessRequest(
        workspace, ModelCredentialRef(workspace, credential), scope, "PRODUCT_API_KEY"
    )
    binding = SecretBinding(
        credential_ref=CredentialRef(workspace_id=workspace, credential_id=credential),
        destination=request.destination,
    )
    calls = []

    class ExactSecretAuthority:
        def require(self, grant, action, target):
            calls.append((grant, action, target))
            if grant != scope or action != "secret.read" or target != binding:
                raise AgentError("permission_denied", "Exact test scope denied")

    reader = composition.CredentialPermissionReader(ExactSecretAuthority())
    assert reader.authorize(request).request is request
    other_workspace = uuid4()
    for changed in (
        replace(request, scope_id=uuid4()),
        replace(request, credential_ref=ModelCredentialRef(workspace, uuid4())),
        replace(
            request,
            workspace_id=other_workspace,
            credential_ref=ModelCredentialRef(other_workspace, credential),
        ),
        replace(request, destination="PRODUCT_OTHER_API_KEY"),
    ):
        with pytest.raises(AgentError) as denied:
            reader.authorize(changed)
        assert denied.value.code == "permission_denied"
    assert calls[0] == (scope, "secret.read", binding)
    assert len(calls) == 5


@pytest.mark.parametrize(
    "code",
    ["runtime_binding_denied", "credential_store_unavailable", "credential_not_found"],
)
def test_product_credential_errors_keep_code_without_private_detail(
    settings_values, no_database_connections, monkeypatch, code
):
    runtime = composition.build_product_runtime(
        ProductSettings.model_validate(settings_values)
    )
    binding = SecretBinding(
        credential_ref=CredentialRef(
            workspace_id=runtime.settings.workspace_id, credential_id=uuid4()
        ),
        destination="PRODUCT_CONTROLLED_API_KEY",
    )
    marker = "TEST_OWNED_PRIVATE_CREDENTIAL_DETAIL"

    def read(request):
        error = ConfigurationError(code)
        error.args = (marker,)
        raise error

    monkeypatch.setattr(runtime.credential_reader, "read", read)
    try:
        with pytest.raises(AgentError) as denied:
            runtime.secret_reader.read(uuid4(), binding)
        assert denied.value.code == code
        rendered = "".join(traceback.format_exception(denied.value))
        assert marker not in rendered
        assert denied.value.__suppress_context__ is True
    finally:
        assert runtime.close(grace_seconds=0) is True


def test_managed_secret_store_uses_stable_namespace_without_reading(
    settings_values, no_database_connections, monkeypatch
):
    namespaces, stores = [], []

    def managed_store(namespace):
        namespaces.append(namespace)
        store = ControlledSecretStore()
        stores.append(store)
        return store

    monkeypatch.setattr(composition, "MacOSKeychainSecretStore", managed_store)
    settings = ProductSettings.model_validate(
        settings_values | {"managed_credentials_enabled": True}
    )
    first = composition.build_product_runtime(settings)
    second = composition.build_product_runtime(settings)
    try:
        assert namespaces == [settings.deployment_namespace] * 2
        assert all(store.reads == [] and store.values == {} for store in stores)
        assert first.status()["execution"]["ready"] is False
        assert second.status()["execution"]["ready"] is False
    finally:
        assert first.close(grace_seconds=0) is True
        assert second.close(grace_seconds=0) is True


def test_unavailable_managed_store_does_not_try_an_environment_fallback():
    store = composition.UnavailableSecretStore()
    with pytest.raises(ConfigurationError) as unavailable:
        store.read(ModelCredentialRef(uuid4(), uuid4()))
    assert unavailable.value.code == "credential_store_unavailable"
