from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid5

from pydantic import TypeAdapter, ValidationError
from sidera_agents.api import AgentCatalog
from sidera_agents.contracts import (
    CapabilityResourceDraft,
    ProfileDraft,
    ProfileRef,
    ResourceRef,
)
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

from sidera_product.auth.local_session import LocalSessions
from sidera_product.config import ProductSettings
from sidera_product.postgres.store import ProductStoreError

from .configuration_store import PostgresConfigurationStore
from .contracts import (
    ConfigurationAction,
    ConfigurationRef,
    ConfigurationRequest,
    ConfigurationResult,
    ConfigurationState,
    canonical_hash,
)


class ConfigurationError(Exception):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class ConfigurationService:
    def __init__(
        self,
        *,
        settings: ProductSettings,
        store: PostgresConfigurationStore,
        sessions: LocalSessions,
        catalog: AgentCatalog,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sessions = sessions
        self.catalog = catalog
        self.clock = clock or (lambda: datetime.now(UTC))

    def _current(self, identity: ControlPlaneIdentity) -> None:
        if (
            identity.actor_type != "user"
            or identity.principal_id != self.settings.local_principal_id
            or self.sessions.identity_for_grant(identity.grant_id) != identity
        ):
            raise ConfigurationError("local_session_required", status_code=401)

    @staticmethod
    def _operation(operation_id: str) -> None:
        if type(operation_id) is not str or not 1 <= len(operation_id) <= 128:
            raise ConfigurationError("configuration_invalid", status_code=422)

    def submit(
        self,
        identity: ControlPlaneIdentity,
        *,
        action: ConfigurationAction,
        operation_id: str,
        draft: ProfileDraft | CapabilityResourceDraft,
        expected_ref: ConfigurationRef | None = None,
    ) -> ConfigurationResult:
        self._current(identity)
        self._operation(operation_id)
        try:
            action = TypeAdapter[ConfigurationAction](
                ConfigurationAction
            ).validate_python(action)
            profile = action.startswith("profile.")
            if profile:
                if not isinstance(draft, ProfileDraft):
                    raise ConfigurationError("configuration_invalid", status_code=422)
                normalized = ProfileDraft.model_validate(
                    draft.model_dump(mode="python", warnings=False)
                )
                refs = (
                    normalized.runtime_ref,
                    *normalized.resource_refs,
                    *(secret.credential_ref for secret in normalized.secret_refs),
                )
            else:
                if not isinstance(draft, CapabilityResourceDraft):
                    raise ConfigurationError("configuration_invalid", status_code=422)
                normalized = CapabilityResourceDraft.model_validate(
                    draft.model_dump(mode="python", warnings=False)
                )
                refs = tuple(secret.credential_ref for secret in normalized.secret_refs)
            if any(ref.workspace_id != self.settings.workspace_id for ref in refs):
                raise ConfigurationError("permission_denied", status_code=403)
            if action.endswith(".revise"):
                if (
                    expected_ref is None
                    or expected_ref.workspace_id != self.settings.workspace_id
                    or isinstance(expected_ref, ProfileRef) is not profile
                ):
                    raise ConfigurationError("permission_denied", status_code=403)
                target = type(expected_ref).model_validate(
                    {**expected_ref.model_dump(), "revision": expected_ref.revision + 1}
                )
            else:
                if expected_ref is not None:
                    raise ConfigurationError("configuration_invalid", status_code=422)
                entity_id = uuid5(
                    self.settings.workspace_id,
                    "sidera.configuration:"
                    + canonical_hash(
                        {
                            "principal": identity.principal_id,
                            "action": action,
                            "operation": operation_id,
                        }
                    ),
                )
                target = (
                    ProfileRef(
                        workspace_id=self.settings.workspace_id,
                        profile_id=entity_id,
                        revision=1,
                    )
                    if profile
                    else ResourceRef(
                        workspace_id=self.settings.workspace_id,
                        resource_id=entity_id,
                        revision=1,
                    )
                )
            canonical = normalized.model_dump(mode="json")
            now = self.clock()
            requested = ConfigurationRequest(
                workspace_id=self.settings.workspace_id,
                principal_id=identity.principal_id,
                operation_id=operation_id,
                action=action,
                target_ref=target,
                expected_ref=expected_ref,
                request_fingerprint=canonical_hash(
                    {
                        "workspace": str(self.settings.workspace_id),
                        "principal": identity.principal_id,
                        "operation": operation_id,
                        "action": action,
                        "expected_ref": None
                        if expected_ref is None
                        else expected_ref.model_dump(mode="json"),
                        "draft": canonical,
                    }
                ),
                draft_fingerprint=canonical_hash(canonical),
                state="dispatching",
                version=1,
                created_at=now,
                updated_at=now,
                reason_code=None,
                result_ref=None,
            )
            record, winner = self.store.claim(requested)
        except ValidationError:
            raise ConfigurationError("configuration_invalid", status_code=422) from None
        except ProductStoreError as error:
            raise ConfigurationError(error.code) from None
        if not winner:
            return self._observe(identity, record)
        self._current(identity)
        try:
            if isinstance(normalized, ProfileDraft) and isinstance(target, ProfileRef):
                saved = (
                    self.catalog.create_profile(
                        target.workspace_id,
                        normalized,
                        grant_id=identity.grant_id,
                        profile_id=target.profile_id,
                    )
                    if expected_ref is None
                    else self.catalog.revise_profile(
                        ProfileRef.model_validate(expected_ref.model_dump()),
                        normalized,
                        grant_id=identity.grant_id,
                    )
                )
            elif isinstance(normalized, CapabilityResourceDraft) and isinstance(
                target, ResourceRef
            ):
                saved = (
                    self.catalog.create_resource(
                        target.workspace_id,
                        normalized,
                        grant_id=identity.grant_id,
                        resource_id=target.resource_id,
                    )
                    if expected_ref is None
                    else self.catalog.revise_resource(
                        ResourceRef.model_validate(expected_ref.model_dump()),
                        normalized,
                        grant_id=identity.grant_id,
                    )
                )
            else:
                raise ConfigurationError("configuration_invalid", status_code=422)
            if (
                saved.ref != target
                or canonical_hash(saved.draft.model_dump(mode="json"))
                != record.draft_fingerprint
            ):
                return self._settle(record, "unknown", "configuration_write_conflict")
            return self._settle(record, "succeeded", None, target)
        except AgentError as error:
            known = {
                "configuration_invalid",
                "revision_conflict",
                "operation_conflict",
                "permission_denied",
                "not_found",
            }
            return self._settle(
                record,
                "rejected" if error.code in known else "unknown",
                error.code
                if error.code in known
                else "configuration_result_unavailable",
            )
        except Exception:  # noqa: BLE001 - post-dispatch uncertainty never permits another catalog write
            return self._settle(record, "unknown", "configuration_result_unavailable")

    def read_original(
        self, identity: ControlPlaneIdentity, operation_id: str
    ) -> ConfigurationResult:
        self._current(identity)
        self._operation(operation_id)
        try:
            record = self.store.get(
                workspace_id=self.settings.workspace_id,
                principal_id=identity.principal_id,
                operation_id=operation_id,
            )
        except ProductStoreError as error:
            raise ConfigurationError(error.code, status_code=503) from None
        if record is None:
            raise ConfigurationError("configuration_request_not_found", status_code=404)
        return self._observe(identity, record)

    def _observe(
        self, identity: ControlPlaneIdentity, record: ConfigurationRequest
    ) -> ConfigurationResult:
        self._current(identity)
        if (
            record.workspace_id != self.settings.workspace_id
            or record.principal_id != identity.principal_id
        ):
            raise ConfigurationError("permission_denied", status_code=403)
        if record.state in {"succeeded", "reconciled", "rejected", "claimed"}:
            return ConfigurationResult.from_request(record)
        try:
            saved = (
                self.catalog.get_profile(record.target_ref, grant_id=identity.grant_id)
                if isinstance(record.target_ref, ProfileRef)
                else self.catalog.get_resource(
                    record.target_ref, grant_id=identity.grant_id
                )
            )
        except AgentError as error:
            if error.code == "permission_denied":
                raise ConfigurationError("permission_denied", status_code=403) from None
            return self._settle(
                record, "unknown", "configuration_result_unavailable", observation=True
            )
        except Exception:  # noqa: BLE001 - unavailable exact lookup stays query-only
            return self._settle(
                record, "unknown", "configuration_result_unavailable", observation=True
            )
        if (
            saved.ref != record.target_ref
            or canonical_hash(saved.draft.model_dump(mode="json"))
            != record.draft_fingerprint
        ):
            return self._settle(
                record, "unknown", "configuration_write_conflict", observation=True
            )
        return self._settle(
            record, "reconciled", None, record.target_ref, observation=True
        )

    def _settle(
        self,
        expected: ConfigurationRequest,
        state: ConfigurationState,
        reason_code: str | None,
        result_ref: ConfigurationRef | None = None,
        *,
        observation: bool = False,
    ) -> ConfigurationResult:
        try:
            record = self.store.settle(
                expected,
                state=state,
                reason_code=reason_code,
                result_ref=result_ref,
                now=self.clock(),
                observation=observation,
            )
            return ConfigurationResult.from_request(record)
        except ProductStoreError:
            return ConfigurationResult(
                operationId=expected.operation_id,
                state="unknown",
                target_ref=expected.target_ref,
                result_ref=None,
                reason_code="configuration_result_unavailable",
            )
