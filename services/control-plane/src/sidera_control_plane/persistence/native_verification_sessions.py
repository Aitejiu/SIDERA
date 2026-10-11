"""Original local checks owned by immutable native setup, not fresh availability."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, not_, or_, select

from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.native_setup import (
    NATIVE_SETUP_POLICY_KEY,
    native_local_slot_ids,
)
from sidera_control_plane.domain.result_integration import VerificationBindingV1
from sidera_control_plane.persistence.models import (
    AssignmentLeaseModel as L,
)
from sidera_control_plane.persistence.models import (
    ExecutionBindingModel as B,
)
from sidera_control_plane.persistence.models import (
    ExecutionEventModel as V,
)
from sidera_control_plane.persistence.models import (
    ExecutionModel as E,
)
from sidera_control_plane.persistence.models import (
    ExecutionSessionModel as S,
)
from sidera_control_plane.persistence.models import (
    GatewayOperationModel as G,
)
from sidera_control_plane.persistence.models import (
    PlanningProfileModel as P,
)
from sidera_control_plane.persistence.models import (
    ResultVerificationResultModel as R,
)


@dataclass(frozen=True)
class OriginalVerification:
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID | None = None
    lease_id: UUID | None = None
    worker_id: UUID | None = None
    fencing_token: int | None = None
    observable: bool = False
    stop_requested: bool = False


def _policy_scope():
    binding = P.policy["system_policy"]["result_integration"]["verification_binding"]
    return (
        P.workspace_id == E.workspace_id,
        P.profile_key == NATIVE_SETUP_POLICY_KEY,
        P.execution_mode == "native_profile",
        or_(
            P.policy["system_policy"]["local_execution_slots"][
                "verification_worker_id"
            ].astext
            == cast(E.assigned_worker_id, String),
            and_(
                binding["adapter_module_ref"].astext
                == cast(B.adapter_module_id, String),
                binding["workspace_module_ref"].astext
                == cast(B.workspace_module_id, String),
            ),
        ),
    )


def _outstanding():
    # Closed/lost CP state alone is not proof that the exact local process ended.
    terminal = exists(
        select(V.id)
        .where(
            V.workspace_id == S.workspace_id,
            V.execution_id == S.execution_id,
            V.session_id == S.id,
            V.event_type.in_(
                ("session.succeeded", "session.failed", "session.canceled")
            ),
            V.payload["provider"].astext == "local_verification",
            V.payload["purpose"].astext == "verification",
            V.payload["status"].astext.in_(
                ("passed", "failed", "environment_blocked", "timed_out", "interrupted")
            ),
            or_(
                V.payload["finished_at"].astext.is_not(None),
                and_(
                    V.payload["pid"].astext.is_(None),
                    V.payload["process_start_identity"].astext.is_(None),
                    V.payload["started_at"].astext.is_(None),
                    or_(
                        and_(
                            V.payload["status"].astext == "environment_blocked",
                            V.payload["failure_code"].astext.in_(
                                (
                                    "workspace_unavailable",
                                    "check_path_invalid",
                                    "log_open_failed",
                                    "source_state_unavailable",
                                    "candidate_source_mismatch",
                                    "source_dirty_before_start",
                                    "process_start_failed",
                                )
                            ),
                        ),
                        and_(
                            V.payload["status"].astext == "failed",
                            V.payload["failure_code"].astext == "prepare_failed",
                            V.payload["exit_code"].astext.is_not(None),
                        ),
                    ),
                ),
            ),
            exists(
                select(R.id).where(
                    R.workspace_id == V.workspace_id,
                    R.execution_id == V.execution_id,
                    cast(R.operation_id, String) == V.payload["operation_id"].astext,
                    R.status.not_in(("unknown", "not_run")),
                )
            ),
        )
        .correlate(S)
    )
    sessions = (S.workspace_id == E.workspace_id, S.execution_id == E.id)
    stop_scope = (
        G.workspace_id == E.workspace_id,
        G.execution_id == E.id,
        G.operation_type == "stop_session",
        G.lease_id == B.lease_id,
        G.fencing_token == B.fencing_token,
        G.module_instance_id == B.adapter_module_id,
        G.config_version == B.adapter_config_version,
        exists(select(S.id).where(*sessions, S.id == G.session_id).correlate(E, G)),
    )
    confirmed_stop = exists(
        select(G.id)
        .where(
            *stop_scope, G.status == "succeeded", G.result_ref.startswith("sequence:")
        )
        .correlate(E, B)
    )
    complete = and_(
        B.id.is_not(None),
        E.status.in_(("succeeded", "failed", "canceled", "lost")),
        E.active_lease_id.is_(None),
        or_(E.stop_requested.is_(False), confirmed_stop),
        not_(
            exists(
                select(G.id).where(*stop_scope, G.status != "succeeded").correlate(E, B)
            )
        ),
        exists(select(S.id).where(*sessions)),
        not_(
            exists(
                select(S.id)
                .where(
                    *sessions,
                    or_(
                        S.status != "closed",
                        not_(terminal),
                        S.worker_id != B.worker_id,
                        S.adapter_module_id != B.adapter_module_id,
                        S.adapter_config_version != B.adapter_config_version,
                        S.workspace_module_id != B.workspace_module_id,
                        S.workspace_config_version != B.workspace_config_version,
                    ),
                )
                .correlate(E, B)
            )
        ),
        not_(
            exists(
                select(L.id).where(
                    L.workspace_id == E.workspace_id,
                    L.execution_id == E.id,
                    L.status == "active",
                )
            )
        ),
    )
    return (
        select(E.id, E.workspace_id)
        .outerjoin(B, and_(B.execution_id == E.id, B.workspace_id == E.workspace_id))
        .where(
            E.purpose == "verification",
            exists(select(P.id).where(*_policy_scope()).correlate(E, B)),
            not_(complete),
        )
    )


class SavedNativeVerificationSessions:
    def __init__(self, uow_factory):
        self._uow_factory = uow_factory

    def has_outstanding(self) -> bool:
        with self._uow_factory() as uow:
            return uow.session.execute(_outstanding().limit(1)).first() is not None

    def page(
        self, *, after: UUID | None, limit: int
    ) -> tuple[OriginalVerification, ...]:
        if not 1 <= limit <= 200:
            raise ValueError(
                "original verification page limit must be between 1 and 200"
            )
        statement = _outstanding()
        if after is not None:
            statement = statement.where(E.id > after)
        with self._uow_factory() as uow:
            rows = uow.session.execute(statement.order_by(E.id).limit(limit)).all()
            return tuple(
                self._original(uow, execution_id, workspace_id)
                for execution_id, workspace_id in rows
            )

    def _original(self, uow, execution_id, workspace_id):
        blocked = OriginalVerification(workspace_id, execution_id)
        execution = uow.executions.get(execution_id, workspace_id)
        binding = uow.bindings.get_for_execution(execution_id, workspace_id)
        session = uow.sessions.get_latest_for_execution(execution_id, workspace_id)
        if execution is None or binding is None or session is None:
            return blocked
        raw_binding = P.policy["system_policy"]["result_integration"][
            "verification_binding"
        ]
        row = uow.session.execute(
            select(P.id, P.version)
            .where(
                P.workspace_id == workspace_id,
                P.profile_key == NATIVE_SETUP_POLICY_KEY,
                P.execution_mode == "native_profile",
                raw_binding["adapter_module_ref"].astext
                == str(binding.adapter_module_id),
                raw_binding["workspace_module_ref"].astext
                == str(binding.workspace_module_id),
                raw_binding["adapter_config_version"].astext
                == str(binding.adapter_config_version),
                raw_binding["workspace_config_version"].astext
                == str(binding.workspace_config_version),
            )
            .order_by(P.version, P.id)
            .limit(1)
        ).first()
        if row is None:
            return blocked
        policy = uow.planning_profiles.get(row.id, workspace_id, version=row.version)
        try:
            saved = VerificationBindingV1.from_mapping(
                policy.system_policy["result_integration"]["verification_binding"]
            )
            _, worker_id = native_local_slot_ids(
                saved.workspace_module_ref, saved.adapter_module_ref
            )
        except (AttributeError, KeyError, TypeError, ValueError, DomainError):
            return blocked
        identity = (
            saved.adapter_module_ref,
            saved.adapter_config_version,
            saved.workspace_module_ref,
            saved.workspace_config_version,
        )
        if (
            execution.assigned_worker_id != worker_id
            or binding.worker_id != worker_id
            or session.worker_id != worker_id
            or (
                session.adapter_module_id,
                session.adapter_config_version,
                session.workspace_module_id,
                session.workspace_config_version,
            )
            != identity
        ):
            return blocked
        for module_id, version, module_type, provider in (
            (
                saved.adapter_module_ref,
                saved.adapter_config_version,
                "worker_adapter",
                "local_verification",
            ),
            (
                saved.workspace_module_ref,
                saved.workspace_config_version,
                "workspace_provider",
                "git-worktree",
            ),
        ):
            instance = uow.modules.get_instance(module_id, workspace_id)
            definition = (
                None
                if instance is None
                else uow.modules.get_definition(instance.definition_id, workspace_id)
            )
            if (
                instance is None
                or instance.config_version != version
                or definition is None
                or definition.module_type != module_type
                or definition.provider != provider
            ):
                return blocked
        worker = uow.workers.get(worker_id, workspace_id)
        lease = uow.leases.get(binding.lease_id, workspace_id)
        observable = bool(
            worker is not None
            and worker.adapter_type == "local_verification"
            and execution.status.value in {"leased", "running"}
            and session.status.value == "active"
            and execution.active_lease_id == binding.lease_id
            and execution.last_fencing_token == binding.fencing_token
            and lease is not None
            and lease.worker_id == worker_id
            and lease.fencing_token == binding.fencing_token
            and lease.status.value == "active"
            and lease.expires_at > datetime.now(UTC)
        )
        return OriginalVerification(
            workspace_id,
            execution_id,
            session.id,
            binding.lease_id,
            worker_id,
            binding.fencing_token,
            observable,
            execution.stop_requested,
        )
