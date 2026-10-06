from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sidera_agents.api import AgentCatalog
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

from .auth.local_session import LocalSessions
from .config import ProductSettings
from .postgres.store import PostgresProductStore, ProductStoreError


class DefaultsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    expected_version: StrictInt = Field(ge=0)
    main_profile_id: UUID | None
    planner_profile_id: UUID | None
    worker_profile_id: UUID | None


class DefaultsView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)
    workspace_id: UUID
    main_profile_id: UUID | None
    planner_profile_id: UUID | None
    worker_profile_id: UUID | None
    version: StrictInt = Field(ge=0)


class DefaultsError(Exception):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class DefaultsService:
    def __init__(
        self,
        *,
        settings: ProductSettings,
        store: PostgresProductStore,
        sessions: LocalSessions,
        catalog: AgentCatalog,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sessions = sessions
        self.catalog = catalog

    def _current(self, identity: ControlPlaneIdentity) -> None:
        if (
            identity.actor_type != "user"
            or identity.principal_id != self.settings.local_principal_id
            or self.sessions.identity_for_grant(identity.grant_id) != identity
        ):
            raise DefaultsError("local_session_required", status_code=401)

    def read(self, identity: ControlPlaneIdentity) -> DefaultsView:
        self._current(identity)
        saved = self.store.get_defaults(self.settings.workspace_id)
        if saved is None:
            return DefaultsView(
                workspace_id=self.settings.workspace_id,
                main_profile_id=None,
                planner_profile_id=None,
                worker_profile_id=None,
                version=0,
            )
        return DefaultsView.model_validate(saved)

    def write(self, identity: ControlPlaneIdentity, body: DefaultsBody) -> DefaultsView:
        self._current(identity)
        selected = {
            value
            for value in (
                body.main_profile_id,
                body.planner_profile_id,
                body.worker_profile_id,
            )
            if value is not None
        }
        if selected:
            profiles = self.catalog.list_profiles(
                self.settings.workspace_id, grant_id=identity.grant_id
            )
            heads = {
                profile.ref.profile_id: profile.ref
                for profile in profiles
                if profile.ref.workspace_id == self.settings.workspace_id
            }
            for profile_id in selected:
                ref = heads.get(profile_id)
                if ref is None:
                    raise DefaultsError("profile_unavailable")
                status = self.catalog.get_profile_status(
                    ref, grant_id=identity.grant_id
                )
                if status.profile_id != profile_id or not status.enabled:
                    raise DefaultsError("profile_unavailable")
        self._current(identity)
        try:
            saved = self.store.write_defaults(
                self.settings.workspace_id,
                expected_version=body.expected_version,
                main_profile_id=body.main_profile_id,
                planner_profile_id=body.planner_profile_id,
                worker_profile_id=body.worker_profile_id,
            )
        except ProductStoreError as error:
            raise DefaultsError(error.code) from None
        return DefaultsView.model_validate(saved)
