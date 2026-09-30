from __future__ import annotations

import ast
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from sidera_model_connections.contracts import (
    BindingPermission,
    ConnectionRef,
    CredentialMetadata,
    CredentialRef,
    ModelConnectionRevision,
    RuntimeAccessRequest,
)
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.resolution import ConnectionResolver
from tests.fakes import FakeBindingPermissionReader, FakeSecretStore, MemoryCatalog

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ROLE_KEYS = {
    "main_agent": ("SIDERA_MAIN_AGENT_KEY", "cfg2-fake-main-key"),
    "worker": ("SIDERA_WORKER_KEY", "cfg2-fake-worker-key"),
    "planner": ("SIDERA_PLANNER_KEY", "cfg2-fake-planner-key"),
}
CORE_MODULES = frozenset(
    sys.stdlib_module_names | {"pydantic", "sidera_model_connections"}
)


def _module_level_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            imports.extend(alias.name for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            imports.append("." * statement.level + (statement.module or ""))
    return tuple(imports)


def test_core_modules_keep_optional_dependencies_out_of_import_closure() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src/sidera_model_connections"
    violations: list[tuple[str, str]] = []

    for path in sorted(source_root.glob("*.py")):
        for imported in _module_level_imports(path):
            if imported.startswith("."):
                continue
            root = imported.split(".", maxsplit=1)[0]
            is_optional_package = root not in CORE_MODULES
            is_optional_adapter = imported.startswith(
                ("sidera_model_connections.http", "sidera_model_connections.postgres")
            )
            if is_optional_package or is_optional_adapter:
                violations.append((path.name, imported))

    assert violations == []


class ObservedEnvironment(Mapping[str, SecretStr]):
    def __init__(self, values: dict[str, SecretStr]) -> None:
        self._values = values
        self.lookups = 0
        self.iterations = 0
        self.length_checks = 0

    def __getitem__(self, key: str) -> SecretStr:
        self.lookups += 1
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        return iter(self._values)

    def __len__(self) -> int:
        self.length_checks += 1
        return len(self._values)


@pytest.mark.parametrize("credential_source", ("macos_keychain", "env"))
@pytest.mark.parametrize(
    ("denial", "expected_code"),
    (
        ("binding", "runtime_binding_denied"),
        ("role", "connection_role_denied"),
        ("target", "connection_target_denied"),
    ),
)
def test_denied_binding_role_and_target_do_not_read_any_secret_source(
    credential_source: str,
    denial: str,
    expected_code: str,
) -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    credential_ref = CredentialRef(WORKSPACE_ID, uuid4())
    env_name, _role_key = ROLE_KEYS["worker"]
    credential = CredentialMetadata(
        ref=credential_ref,
        alias="worker credential",
        source=credential_source,
        status="active",
        env_name=env_name if credential_source == "env" else None,
        replaces_ref=None,
        created_at=now,
        version=1,
    )
    requested_role = "worker" if denial == "role" else "main_agent"
    request = RuntimeAccessRequest(WORKSPACE_ID, requested_role, uuid4())
    connection = ModelConnectionRevision(
        ref=ConnectionRef(WORKSPACE_ID, uuid4(), 1),
        name="boundary probe",
        provider_id="example",
        protocol="openai_compatible",
        base_url="https://api.example.test/v1",
        network_scope="public",
        allowed_roles=frozenset({"main_agent"}),
        credential_ref=credential_ref,
        created_at=now,
    )
    catalog = MemoryCatalog()
    catalog.credentials[credential_ref] = credential
    catalog.create_connection(connection)
    store = FakeSecretStore()
    store.put_new(credential_ref, SecretStr(ROLE_KEYS["main_agent"][1]))
    environment = ObservedEnvironment(
        {
            name: SecretStr(key)
            for name, key in ROLE_KEYS.values()
        }
    )

    permissions = {}
    if denial != "binding":
        origins = (
            frozenset({"https://other.example.test"})
            if denial == "target"
            else frozenset({"https://api.example.test"})
        )
        permissions[request] = BindingPermission(
            request,
            connection.ref,
            frozenset({"openai_compatible"}),
            origins,
        )
    resolver = ConnectionResolver(
        catalog,
        FakeBindingPermissionReader(permissions),
        store,
        environment,
    )

    with pytest.raises(ConfigurationError) as rejected:
        resolver.resolve(request)

    assert rejected.value.code == expected_code
    assert environment.lookups == 0
    assert environment.iterations == 0
    assert environment.length_checks == 0
    assert not any(action == "read" for action, _ref in store.calls)
