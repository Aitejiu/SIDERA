# Sidera Model Connections

`sidera-model-connections` provides workspace-scoped connection and credential
contracts, validation, persistence adapters, and an optional management router.
It does not configure a model provider or change an agent's active connection.

## Install

From a source checkout, run these commands in `packages/model-connections`:

```sh
python3.12 -m pip install .
python3.12 -m pip install ".[postgres]"
python3.12 -m pip install ".[postgres,http]"
```

Add the `[macos]` extra on a supported macOS installation:

```sh
python3.12 -m pip install ".[macos]"
```

The core install requires Pydantic. `postgres` adds SQLAlchemy, psycopg, and
Alembic; `http` adds FastAPI; `macos` adds PyObjC Security. Install only the
adapters used by the host application.

## PostgreSQL

Set `SIDERA_CONFIGURATION_DATABASE_URL` through the host's trusted
configuration, then create a catalog with a SQLAlchemy session factory:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sidera_model_connections.postgres import PostgresConnectionCatalog

engine = create_engine(database_url)
catalog = PostgresConnectionCatalog(sessionmaker(engine, expire_on_commit=False))
```

Apply migrations explicitly from this directory:

```sh
alembic -c alembic.ini upgrade head
```

The package does not migrate a database at application startup; its catalog
uses the `sidera_configuration` schema.

## Credentials And Resolution

Use `CredentialService.register_env(workspace_id, alias, env_name)` to register
an environment source. It stores the variable name, not its value. For managed
credentials on macOS, provide a stable deployment namespace to
`MacOSKeychainSecretStore`:

```python
from pydantic import SecretStr
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.macos_keychain import MacOSKeychainSecretStore

secret_store = MacOSKeychainSecretStore(deployment_namespace)
credential_service = CredentialService(catalog, secret_store)
write = credential_service.save_managed(
    workspace_id, operation_id, "Provider key", SecretStr(provider_key)
)
```

Keep the original `operation_id` when checking or recovering an uncertain write.
The current no-interaction check covers macOS 26.6.2 arm64, Python 3.12.14, and
PyObjC Security 12.2.2. It does not establish support for other macOS/Python
combinations or signed data-protection Keychain deployments.

The role owner must supply a permission reader that validates its real binding.
Pass only explicitly selected environment sources to `ConnectionResolver`:

```python
from collections.abc import Mapping
from pydantic import SecretStr
from sidera_model_connections.resolution import ConnectionResolver

selected_sources: Mapping[str, SecretStr] = {
    "SIDERA_PROVIDER_KEY": SecretStr(provider_key),
}
resolver = ConnectionResolver(catalog, permission_reader, secret_store, selected_sources)
resolved = resolver.resolve(runtime_access_request)
```

A missing selected variable stays unresolved; another environment variable is
not tried as a fallback. `resolved.secret` is a `SecretStr` for in-memory use by
the caller. Do not log, serialize, or persist a resolved connection.

## Management Router

An installed `[http]` extra exposes `create_management_router(...)`. The host
must supply its authorizer, loopback binding, CORS policy, and token lifecycle.
The optional router is not mounted into Sidera's current Web settings.

## Limits

The package does not call a Provider, supply a model transport, discover native
agent credentials, or implement the Main Agent, Worker, or Planner owners.
Saving a connection does not select it for a role or prove that a model is
reachable. A database backup contains metadata and credential references only;
managed Keychain items require separate OS-level recovery. The package does not
export or restore Keychain key material.
