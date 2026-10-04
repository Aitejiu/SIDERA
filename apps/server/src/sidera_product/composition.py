from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid5

from fastapi import Request
from pydantic import SecretStr
from sidera_agents import bootstrap
from sidera_agents.contracts import (
    CredentialRef,
    ExecutionWorkspace,
    ProfileRef,
    ResourceRef,
    RuntimeRef,
    SecretBinding,
    SourceContent,
    SourceRef,
    ToolBinding,
)
from sidera_agents.errors import AgentError
from sidera_agents.ports import (
    ContextSourceReader,
    DenyToolConnectionReader,
    ExecutionAuthorizer,
    HarnessAdapter,
    ToolConnectionReader,
)
from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.execution_authority import PlanningActionTarget
from sidera_control_plane.composition import native_runtime
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    ControlPlaneRequestAuthorizer,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_model_connections.contracts import CredentialRef as ModelCredentialRef
from sidera_model_connections.errors import ConfigurationError
from sidera_model_connections.macos_keychain import MacOSKeychainSecretStore
from sidera_model_connections.native_access import (
    NativeCredentialAccessPermission,
    NativeCredentialAccessRequest,
    NativeCredentialReader,
)
from sidera_model_connections.ports import SecretStore
from sidera_model_connections.postgres import PostgresConnectionCatalog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import ProductSettings
from .lifecycle import ProductRuntime
from .postgres import PostgresProductStore


def deny() -> NoReturn:
    raise AgentError("permission_denied", "permission_denied: Access denied.")


class ProductRequestAuthorizer:
    """Registered internal scope, distinct from CP's native execution grants."""

    def __init__(
        self,
        settings: ProductSettings,
        trusted: ControlPlaneRequestAuthorizer | None = None,
    ) -> None:
        self.settings = settings
        self.trusted = trusted
        self.background_identity = ControlPlaneIdentity(
            principal_id=settings.local_principal_id,
            grant_id=uuid5(
                settings.deployment_namespace,
                "sidera.control-plane.background:" + str(settings.workspace_id),
            ),
            actor_type="system",
        )

    def authenticate_request(self, request: Request) -> ControlPlaneIdentity | None:
        identity = (
            None if self.trusted is None else self.trusted.authenticate_request(request)
        )
        if (
            identity is None
            or identity.actor_type not in {"user", "agent"}
            or identity.grant_id == self.background_identity.grant_id
        ):
            return None
        return identity

    def identity_for_grant(self, grant_id: UUID) -> ControlPlaneIdentity | None:
        if grant_id == self.background_identity.grant_id:
            return self.background_identity
        return (
            None if self.trusted is None else self.trusted.identity_for_grant(grant_id)
        )

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        if grant_id != self.background_identity.grant_id:
            if self.trusted is None:
                deny()
            self.trusted.require(grant_id, action, target)
            return
        workspace = self.settings.workspace_id
        if (
            action in {"profile.list", "resource.list", "runtime.list"}
            and type(target) is UUID
            and target == workspace
        ) or (
            (
                (action == "profile.read" and isinstance(target, ProfileRef))
                or (action == "resource.read" and isinstance(target, ResourceRef))
                or (action == "runtime.read" and isinstance(target, RuntimeRef))
            )
            and target.workspace_id == workspace
        ):
            return
        if isinstance(target, PlanningActionTarget):
            self._require_planning(action, target)
            return
        if (
            isinstance(target, AssignmentTarget)
            and target.workspace_id == workspace
            and target.principal_id == self.settings.local_principal_id
            and type(target.target_id) is UUID
            and target.target_type in {"subtask", "worker_item"}
            and action
            in {
                "profile.read",
                "runtime.read",
                "resource.read",
                "run.start",
                "workspace.execute",
                "artifact.read",
                "source.read",
                "secret.read",
                "tool.execute",
                "run.input",
                "run.recover",
                "run.interrupt",
            }
        ):
            return
        deny()

    def _require_planning(self, action: str, target: PlanningActionTarget) -> None:
        if (
            target.workspace_id != self.settings.workspace_id
            or target.principal_id != self.settings.local_principal_id
            or type(target.profile_ref) is not ProfileRef
            or target.profile_ref.workspace_id != target.workspace_id
            or type(target.planning_profile_version) is not int
            or target.planning_profile_version < 1
        ):
            deny()
        value = target.target
        if action in {"planning.prepare", "profile.read", "run.start"}:
            if isinstance(value, ProfileRef) and value == target.profile_ref:
                return
        elif (action == "runtime.read" and isinstance(value, RuntimeRef)) or (
            action == "resource.read" and isinstance(value, ResourceRef)
        ):
            if value.workspace_id == target.workspace_id:
                return
        elif action == "source.read" and isinstance(value, ContextPackageManifestV1):
            if (
                value.workspace_id == target.workspace_id
                and value.purpose is ContextPurpose.PLANNING_RUN
                and value.consumer_type is ContextConsumerType.PLANNER
                and value.consumer_id == target.planning_run_id
                and value.worker_item_id is None
                and value.context_version is None
                and value.budget.profile_id == target.planning_profile_id
                and value.budget.profile_version == target.planning_profile_version
            ):
                return
        elif action == "source.read" and isinstance(value, SourceRef):
            if value.provider == "control-plane":
                return
        elif action == "secret.read" and isinstance(
            value, (SecretBinding, CredentialRef)
        ):
            credential = (
                value.credential_ref if isinstance(value, SecretBinding) else value
            )
            if credential.workspace_id == target.workspace_id:
                return
        elif action == "tool.execute" and isinstance(value, ToolBinding):
            if value.connection_id is not None or (
                value.resource_ref is not None
                and value.resource_ref.workspace_id == target.workspace_id
            ):
                return
        elif action == "workspace.execute" and isinstance(value, ExecutionWorkspace):
            directory = Path(value.directory)
            try:
                owned = (
                    directory.is_absolute()
                    and ".." not in directory.parts
                    and directory.resolve().is_relative_to(
                        self.settings.workspace_root.resolve()
                    )
                )
            except (OSError, RuntimeError, ValueError):
                owned = False
            if owned and value.writable is True and value.artifact_directory is None:
                return
        deny()


class AuthorityRouter:
    def __init__(
        self,
        host: ProductRequestAuthorizer,
        product: ExecutionAuthorizer | None = None,
    ) -> None:
        self.host = host
        self.product = product
        self.control_plane: ExecutionAuthorizer | None = None

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        if not isinstance(
            target,
            (
                UUID,
                ProfileRef,
                ResourceRef,
                RuntimeRef,
                ExecutionWorkspace,
                SourceRef,
                SecretBinding,
                CredentialRef,
                ToolBinding,
            ),
        ):
            deny()
        if grant_id == self.host.background_identity.grant_id:
            self.host.require(grant_id, action, target)
            return
        if self.product is not None:
            try:
                self.product.require(grant_id, action, target)
                return
            except AgentError as error:
                if error.code != "permission_denied":
                    raise
        if self.control_plane is None:
            deny()
        # Never resolve a CP-minted grant via the host identity lookup or recurse
        # through this router when CP checks its original upstream authority.
        self.control_plane.require(grant_id, action, target)


class SourceRouter:
    def __init__(self, product: ContextSourceReader | None = None) -> None:
        self.product = product
        self.control_plane: ContextSourceReader | None = None

    def read(self, grant_id: UUID, ref: SourceRef) -> SourceContent:
        if ref.provider == "control-plane" and self.control_plane is not None:
            return self.control_plane.read(grant_id, ref)
        if self.product is not None:
            return self.product.read(grant_id, ref)
        if ref.provider == "knowledge":
            raise AgentError("source_unavailable", "Knowledge source is unavailable.")
        deny()


class UnavailableSecretStore:
    def put_new(self, ref: ModelCredentialRef, value: SecretStr) -> None:
        raise ConfigurationError("credential_store_unavailable")

    def read(self, ref: ModelCredentialRef) -> SecretStr:
        raise ConfigurationError("credential_store_unavailable")

    def delete_owned(self, ref: ModelCredentialRef) -> None:
        raise ConfigurationError("credential_store_unavailable")


class CredentialPermissionReader:
    def __init__(self, authority: ExecutionAuthorizer) -> None:
        self.authority = authority

    def authorize(
        self, request: NativeCredentialAccessRequest
    ) -> NativeCredentialAccessPermission:
        self.authority.require(
            request.scope_id,
            "secret.read",
            SecretBinding(
                credential_ref=CredentialRef(
                    workspace_id=request.workspace_id,
                    credential_id=request.credential_ref.credential_id,
                ),
                destination=request.destination,
            ),
        )
        return NativeCredentialAccessPermission(request)


class AgentSecretReader:
    def __init__(self, reader: NativeCredentialReader) -> None:
        self.reader = reader

    def read(self, grant_id: UUID, binding: SecretBinding) -> SecretStr:
        try:
            return self.reader.read(
                NativeCredentialAccessRequest(
                    workspace_id=binding.credential_ref.workspace_id,
                    credential_ref=ModelCredentialRef(
                        workspace_id=binding.credential_ref.workspace_id,
                        credential_id=binding.credential_ref.credential_id,
                    ),
                    scope_id=grant_id,
                    destination=binding.destination,
                )
            )
        except ConfigurationError as error:
            raise AgentError(error.code, "Credential access is unavailable.") from None


def build_product_runtime(
    settings: ProductSettings,
    *,
    harnesses: Iterable[HarnessAdapter] | None = None,
    request_authorizer: ControlPlaneRequestAuthorizer | None = None,
    product_authorizer: ExecutionAuthorizer | None = None,
    product_sources: ContextSourceReader | None = None,
    secret_store: SecretStore | None = None,
    tool_connections: ToolConnectionReader | None = None,
) -> ProductRuntime:
    host = ProductRequestAuthorizer(settings, request_authorizer)
    authority = AuthorityRouter(host, product_authorizer)
    sources = SourceRouter(product_sources)
    store = PostgresProductStore(settings.product_database_url.get_secret_value())
    credential_engine = create_engine(
        settings.credential_database_url.get_secret_value(),
        pool_pre_ping=True,
        echo=False,
        hide_parameters=True,
    )
    catalog = PostgresConnectionCatalog(
        sessionmaker(credential_engine, expire_on_commit=False)
    )
    if secret_store is None:
        secret_store = (
            MacOSKeychainSecretStore(settings.deployment_namespace)
            if settings.managed_credentials_enabled
            else UnavailableSecretStore()
        )
    credential_reader = NativeCredentialReader(
        catalog,
        CredentialPermissionReader(authority),
        secret_store,
        dict(settings.selected_secrets),
    )
    secrets = AgentSecretReader(credential_reader)
    agent_store = bootstrap.open_postgres_store(
        settings.agent_database_url.get_secret_value()
    )
    agents = bootstrap.create_agent_service(
        store=agent_store,
        registry=bootstrap.create_harness_registry(harnesses),
        authorizer=authority,
        sources=sources,
        secrets=secrets,
        clock=lambda: datetime.now(UTC),
        tool_connections=tool_connections or DenyToolConnectionReader(),
    )
    try:
        cp = native_runtime.create_control_plane_runtime(
            settings.control_plane_settings(),
            agent_execution=agents.execution,
            agent_catalog=agents.catalog,
            agent_observations=agents.observations,
            source_reader=sources,
            request_authorizer=host,
            agent_runtimes=agents.runtimes,
        )
    except Exception:  # noqa: BLE001 - inert construction failure must release owned engines
        agents.close(timeout=0)
        agent_store.close()
        credential_engine.dispose()
        store.close()
        raise RuntimeError("product_composition_failed") from None
    authority.control_plane = cp.execution_authorizer
    sources.control_plane = cp.context_source_reader
    return ProductRuntime(
        settings=settings,
        product_store=store,
        agent_store=agent_store,
        credential_engine=credential_engine,
        credential_catalog=catalog,
        credential_reader=credential_reader,
        secret_reader=secrets,
        agents=agents,
        control_plane=cp,
        authority=authority,
        sources=sources,
        request_authorizer=host,
        background_identity=host.background_identity,
    )
