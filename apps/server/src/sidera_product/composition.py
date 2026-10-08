from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from fastapi import Request
from pydantic import SecretStr, TypeAdapter, ValidationError
from sidera_agents import bootstrap
from sidera_agents.contracts import (
    CatalogStatusTarget,
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
    ExecutionAuthorizer,
    HarnessAdapter,
    ToolConnectionReader,
)
from sidera_control_plane.application.assignments import AssignmentTarget
from sidera_control_plane.application.execution_authority import PlanningActionTarget
from sidera_control_plane.application.planning_commands import PlanningCommandTarget
from sidera_control_plane.application.request_authority import (
    ControlPlaneRequestTarget,
    is_result_acceptance_basis_query,
)
from sidera_control_plane.composition import native_runtime
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    ControlPlaneRequestAuthorizer,
    ProfileWorkRunTarget,
    ProfileWorkTarget,
    WorkerProgressTarget,
)
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageManifestV1,
    ContextPurpose,
)
from sidera_control_plane.domain.native_setup import WorkspaceSetupRequest
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

from .agents.configuration import ConfigurationService
from .agents.configuration_store import PostgresConfigurationStore
from .auth.grants import ProductGrants, ToolConnectionResolver, VerifiedToolGrant
from .auth.local_session import SESSION_COOKIE, LocalSessions
from .chat.service import ChatService
from .config import ProductSettings
from .defaults import DefaultsService
from .lifecycle import ProductRuntime
from .postgres import PostgresProductStore
from .postgres.authority_store import PostgresAuthorityStore, ReceiptStore
from .postgres.chat_store import PostgresChatStore


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
        self.local_sessions: LocalSessions | None = None
        self.grants: ProductGrants | None = None
        self.tool_receipts: ReceiptStore | None = None
        self.registered_harness_ids: frozenset[str] = frozenset()
        self.background_identity = ControlPlaneIdentity(
            principal_id=settings.local_principal_id,
            grant_id=uuid5(
                settings.deployment_namespace,
                "sidera.control-plane.background:" + str(settings.workspace_id),
            ),
            actor_type="system",
        )

    def authenticate_request(self, request: Request) -> ControlPlaneIdentity | None:
        authorization = request.headers.get("authorization")
        if authorization is not None:
            if (
                SESSION_COOKIE in request.cookies
                or self.grants is None
                or not authorization.lower().startswith("bearer ")
                or request.headers.get("host")
                not in {
                    urlsplit(self.settings.local_url).netloc,
                    urlsplit(self.settings.web_origin).netloc,
                }
            ):
                return None
            try:
                return self.grants.verify(
                    SecretStr(authorization[7:]), require_running=True
                ).identity
            except AgentError:
                return None
        if SESSION_COOKIE in request.cookies and self.local_sessions is not None:
            return self.local_sessions.authenticate_request(request)
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
        trusted = (
            None if self.trusted is None else self.trusted.identity_for_grant(grant_id)
        )
        if trusted is not None:
            return trusted
        local = (
            None
            if self.local_sessions is None
            else self.local_sessions.identity_for_grant(grant_id)
        )
        if local is not None:
            return local
        if self.grants is not None:
            record = self.grants.store.find_grant(grant_id=grant_id)
            if record is not None:
                try:
                    return self.grants.verify_grant(grant_id).identity
                except AgentError:
                    return None
        return None

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        if grant_id != self.background_identity.grant_id:
            if self.local_sessions is not None:
                identity = self.local_sessions.identity_for_grant(grant_id)
                if identity is not None:
                    self._require_local_user(identity, action, target)
                    return
            if isinstance(target, (ProfileWorkTarget, ProfileWorkRunTarget)):
                deny()
            if (
                self.grants is not None
                and self.grants.store.find_grant(grant_id=grant_id) is not None
            ):
                self._require_tool(self.grants.verify_grant(grant_id), action, target)
                return
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

    def _require_tool(
        self, verified: VerifiedToolGrant, action: str, target: object
    ) -> None:
        record = verified.record
        if (
            self.grants is None
            or record.workspace_id != self.settings.workspace_id
            or record.principal_id != self.settings.local_principal_id
        ):
            deny()
        if (
            action == "workspace.setup.read"
            and record.role == "main"
            and type(target) is UUID
            and target == record.workspace_id
        ):
            return
        if isinstance(target, PlanningCommandTarget):
            if (
                action != "planning.submit"
                or record.role != "main"
                or target.workspace_id != record.workspace_id
                or target.principal_id != record.principal_id
                or target.planning_request_id is not None
                or target.assignee_bindings
                or type(target.planning_profile_id) is not UUID
                or type(target.planning_profile_version) is not int
                or target.planning_profile_version < 1
            ):
                deny()
            self.grants.require(
                record.grant_id, "profile.read", target.planner_agent_ref
            )
            return
        if isinstance(target, WorkerProgressTarget) and action == "progress.report":
            self._require_progress(verified, target)
            return
        if not isinstance(target, ControlPlaneRequestTarget) or (
            target.workspace_id != record.workspace_id
            or target.principal_id != record.principal_id
        ):
            deny()
        if (
            action == "control_plane.query"
            and target.method in {"GET", "HEAD"}
            and target.command is None
        ):
            self._require_tool_read(verified, target)
            return
        command = target.command
        if (
            command is None
            or target.method != "POST"
            or target.route != "/api/v1/commands"
            or action != "control_plane.command:" + command.command_type
            or command.workspace_id != record.workspace_id
            or command.actor_type != "agent"
            or command.actor_id != record.principal_id
        ):
            deny()
        if record.role == "main":
            if (
                command.command_type == "SubmitTaskGoal"
                and command.target_type == "workspace"
                and command.target_id is None
            ):
                return
            if (
                command.command_type == "SubmitPlanRevision"
                and command.target_type == "task"
                and type(command.target_id) is UUID
            ):
                return
        if (
            record.role == "worker"
            and command.command_type == "ReportWorkerProgress"
            and command.target_type == "execution"
        ):
            if type(command.expected_version) is not int or any(
                type(command.payload.get(key)) is not int
                for key in ("binding_version", "fencing_token")
            ):
                deny()
            try:
                progress = TypeAdapter(WorkerProgressTarget).validate_python(
                    {
                        "workspace_id": record.workspace_id,
                        "principal_id": record.principal_id,
                        "execution_id": command.target_id,
                        "execution_version": command.expected_version,
                        "native_grant_id": command.payload.get("authority_id"),
                        **{
                            key: command.payload.get(key)
                            for key in (
                                "worker_item_id",
                                "connection_id",
                                "binding_version",
                                "lease_id",
                                "fencing_token",
                                "run_id",
                                "response_id",
                            )
                        },
                    }
                )
            except (ValueError, ValidationError):
                deny()
            self._require_progress(verified, progress)
            return
        deny()

    @staticmethod
    def _require_progress(
        verified: VerifiedToolGrant, target: WorkerProgressTarget
    ) -> None:
        scope = verified.scope
        if (
            verified.record.role != "worker"
            or scope is None
            or (
                target.workspace_id,
                target.principal_id,
                target.execution_id,
                target.worker_item_id,
                target.native_grant_id,
                target.connection_id,
                target.execution_version,
                target.binding_version,
                target.lease_id,
                target.fencing_token,
                target.run_id,
                target.response_id,
            )
            != (
                scope.workspace_id,
                scope.principal_id,
                scope.execution_id,
                scope.worker_item_id,
                scope.native_grant_id,
                scope.connection_id,
                scope.execution_version,
                scope.binding_version,
                scope.lease_id,
                scope.fencing_token,
                verified.run.run_id,
                verified.run.active_response_id,
            )
        ):
            deny()

    def _require_tool_read(
        self, verified: VerifiedToolGrant, target: ControlPlaneRequestTarget
    ) -> None:
        record = verified.record
        paths = dict(target.path_parameters)
        if "workspace_id" in paths and paths["workspace_id"] != str(
            record.workspace_id
        ):
            deny()
        if target.route == "/api/v1/tasks" and not paths and record.role == "main":
            return
        if target.route == "/api/v1/tasks/{task_id}" and set(paths) == {"task_id"}:
            try:
                root = UUID(paths["task_id"])
            except ValueError:
                deny()
            if record.role == "main" or record.root_task_id == root:
                return
        if (
            target.route
            == "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/active-plan"
            and set(paths) == {"workspace_id", "root_task_id"}
            and record.role == "main"
        ):
            return
        if (
            target.route
            == "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}"
            and set(paths) == {"workspace_id", "request_id"}
            and (
                record.role == "main"
                or (
                    record.role == "planner"
                    and paths["request_id"] == str(record.planning_request_id)
                )
            )
        ):
            return
        if (
            target.route
            in {
                "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace",
                "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity",
            }
            and set(paths) == {"workspace_id", "worker_item_id"}
            and record.role == "worker"
            and paths["worker_item_id"] == str(record.worker_item_id)
        ):
            return
        if (
            target.route == "/api/v1/workspaces/{workspace_id}/commands/{command_id}"
            and set(paths) == {"workspace_id", "command_id"}
            and self.tool_receipts is not None
        ):
            try:
                command_id = UUID(paths["command_id"])
            except ValueError:
                deny()
            receipt = self.tool_receipts.find_receipt(
                grant_id=record.grant_id, command_id=command_id
            )
            if (
                receipt is not None
                and receipt.run_id == verified.run.run_id
                and receipt.response_id == verified.run.active_response_id
            ):
                return
        deny()

    def _require_local_user(
        self, identity: ControlPlaneIdentity, action: str, target: object
    ) -> None:
        workspace = self.settings.workspace_id
        if (
            identity.actor_type != "user"
            or identity.principal_id != self.settings.local_principal_id
        ):
            deny()
        if type(target) is ProfileWorkTarget:
            if (
                action == "assignment.read"
                and type(target.workspace_id) is UUID
                and target.workspace_id == workspace
                and type(target.profile_id) is UUID
                and target.principal_id == identity.principal_id
            ):
                return
            deny()
        if type(target) is ProfileWorkRunTarget:
            if (
                action == "assignment.read"
                and type(target.workspace_id) is UUID
                and target.workspace_id == workspace
                and type(target.work_id) is UUID
                and target.principal_id == identity.principal_id
                and target.kind in {"assignment", "execution", "planning"}
                and (
                    target.target_type in {"subtask", "worker_item"}
                    if target.kind == "assignment"
                    else target.target_type is None
                )
            ):
                return
            deny()
        if isinstance(target, CatalogStatusTarget):
            if (
                type(target.workspace_id) is UUID
                and target.workspace_id == workspace
                and type(target.entity_id) is UUID
                and (target.entity, action)
                in {
                    ("profile", "profile.enable"),
                    ("profile", "profile.disable"),
                    ("resource", "resource.enable"),
                    ("resource", "resource.disable"),
                }
            ):
                return
            deny()
        if action == "runtime.describe":
            if type(target) is str and target in self.registered_harness_ids:
                return
            deny()
        if isinstance(target, ControlPlaneRequestTarget):
            if (
                target.workspace_id != workspace
                or target.principal_id != identity.principal_id
            ):
                deny()
            if action == "control_plane.query" and (
                target.method in {"GET", "HEAD"}
                or is_result_acceptance_basis_query(target)
            ):
                return
            command = target.command
            if command is not None and (
                action == "control_plane.command:" + command.command_type
                and command.workspace_id == workspace
                and command.actor_type == "user"
                and command.actor_id == identity.principal_id
                and command.command_type
                in {
                    "SubmitTaskGoal",
                    "SubmitPlanRevision",
                    "CancelPlanningRequest",
                    "AnswerPlanningClarification",
                    "RequestProposalRevision",
                    "RejectPlanningProposal",
                    "ConfirmPlanningProposal",
                    "ConfirmPlanRevision",
                    "SetSubtaskAssignee",
                    "SetWorkerItemAssignee",
                    "UpdateUnstartedAgentBinding",
                    "ConfirmProfileHandoff",
                    "ConfirmWorkerHandoff",
                    "ConfirmWorkerTransitionStopped",
                    "ApproveCollaborationReleaseProposal",
                    "AuthorizeAdditionalRework",
                    "CompleteTask",
                    "SubmitResult",
                    "AcceptResult",
                    "RejectResult",
                    "CreateTaskInput",
                    "RetryTaskInput",
                    "CancelTaskInput",
                    "RequestExecutionStop",
                    "ReconcileWorkerTransition",
                    "RecoverWorkerTransition",
                    "SendExecutionInput",
                    "UpdatePendingExecutionInput",
                    "CancelExecutionInput",
                    "RetryBlockedExecutionInput",
                    "PrepareProfileHandoff",
                    "CancelWorkerHandoff",
                    "RecoverWorkerHandoff",
                    "RetryBlockedPlanningRun",
                }
            ):
                return
            deny()
        if (
            action == "workspace.setup.read"
            and type(target) is UUID
            and target == workspace
        ):
            return
        if (
            action == "workspace.configure"
            and isinstance(target, WorkspaceSetupRequest)
            and target.workspace_id == workspace
        ):
            return
        if (
            isinstance(target, PlanningCommandTarget)
            and action
            in {
                "planning.submit",
                "planning.answer",
                "planning.revise",
                "planning.confirm",
                "planning.cancel",
                "planning.reject",
                "planning.write",
            }
            and target.workspace_id == workspace
            and target.principal_id == identity.principal_id
            and target.planner_agent_ref.workspace_id == workspace
        ):
            return
        if (
            isinstance(target, AssignmentTarget)
            and target.workspace_id == workspace
            and target.principal_id == identity.principal_id
            and action
            in {
                "assignment.read",
                "assignment.write",
                "profile.read",
                "runtime.read",
                "resource.read",
            }
            and target.target_type in {"subtask", "worker_item", "execution"}
            and type(target.target_id) is UUID
        ):
            return
        if (
            type(target) is UUID
            and target == workspace
            and action
            in {
                "profile.list",
                "profile.create",
                "resource.list",
                "resource.create",
                "runtime.list",
                "runtime.register",
                "runtime.discover",
                "runtime.descriptor.list",
            }
        ):
            return
        if (
            (
                isinstance(target, ProfileRef)
                and action in {"profile.read", "profile.revise"}
            )
            or (
                isinstance(target, ResourceRef)
                and action in {"resource.read", "resource.revise"}
            )
            or (
                isinstance(target, RuntimeRef)
                and action in {"runtime.read", "runtime.refresh", "runtime.configure"}
            )
        ) and target.workspace_id == workspace:
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
        self.grants: ProductGrants | None = None
        self.control_plane: ExecutionAuthorizer | None = None

    def require(self, grant_id: UUID, action: str, target: object) -> None:
        known_descriptor = (
            action == "runtime.describe"
            and type(target) is str
            and target in self.host.registered_harness_ids
        )
        if not known_descriptor and not isinstance(
            target,
            (
                UUID,
                CatalogStatusTarget,
                AssignmentTarget,
                ProfileWorkTarget,
                ProfileWorkRunTarget,
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
        if self.host.local_sessions is not None and (
            self.host.local_sessions.store.find_session(session_id=grant_id) is not None
        ):
            self.host.require(grant_id, action, target)
            return
        if isinstance(
            target, (CatalogStatusTarget, ProfileWorkTarget, ProfileWorkRunTarget)
        ) or action in {
            "assignment.read",
            "runtime.describe",
            "runtime.descriptor.list",
        }:
            deny()
        if self.product is not None:
            try:
                self.product.require(grant_id, action, target)
                return
            except AgentError as error:
                if error.code != "permission_denied":
                    raise
        if (
            self.grants is not None
            and self.grants.store.find_grant(grant_id=grant_id) is not None
        ):
            self.grants.require(grant_id, action, target)
            return
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
    store = PostgresProductStore(settings.product_database_url.get_secret_value())
    authority_store = PostgresAuthorityStore(store.engine)
    local_sessions = LocalSessions(settings, authority_store)
    tool_reader = ToolConnectionResolver(settings, tool_connections)
    host = ProductRequestAuthorizer(settings, request_authorizer)
    host.local_sessions = local_sessions
    host.tool_receipts = authority_store
    authority = AuthorityRouter(host, product_authorizer)
    sources = SourceRouter(product_sources)
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
        tool_connections=tool_reader,
    )
    host.registered_harness_ids = frozenset(agents.runtimes.registered_harness_ids())
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
    grants = ProductGrants(
        settings,
        authority_store,
        local_sessions,
        catalog=agents.catalog,
        runtimes=agents.runtimes,
        execution=agents.execution,
        read_cp_scope=cp.read_task_tool_scope,
        parent_authorizer=host,
    )
    host.grants = authority.grants = tool_reader.grants = grants
    chat = ChatService(
        settings=settings,
        product_store=store,
        store=PostgresChatStore(store.engine),
        sessions=local_sessions,
        catalog=agents.catalog,
        runtimes=agents.runtimes,
        execution=agents.execution,
        grants=grants,
        authority_store=authority_store,
        receipt_store=authority_store,
    )
    return ProductRuntime(
        settings=settings,
        product_store=store,
        authority_store=authority_store,
        local_sessions=local_sessions,
        grants=grants,
        chat=chat,
        configuration=ConfigurationService(
            settings=settings,
            store=PostgresConfigurationStore(store.engine),
            sessions=local_sessions,
            catalog=agents.catalog,
        ),
        defaults=DefaultsService(
            settings=settings,
            store=store,
            sessions=local_sessions,
            catalog=agents.catalog,
        ),
        task_tool_connections=tool_reader,
        agent_store=agent_store,
        credential_engine=credential_engine,
        credential_catalog=catalog,
        secret_store=secret_store,
        credential_reader=credential_reader,
        secret_reader=secrets,
        agents=agents,
        control_plane=cp,
        authority=authority,
        sources=sources,
        request_authorizer=host,
        background_identity=host.background_identity,
    )
