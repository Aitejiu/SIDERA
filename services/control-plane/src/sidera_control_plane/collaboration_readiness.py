from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import and_, select, text

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_governance_commands import (
    CollaborationGovernanceCommandHandler,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeFactory,
    RegistryCollaborationRuntimeFactory,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.collaboration import (
    CollaborationRuntimeBindingSnapshot,
)
from sidera_control_plane.domain.modules import (
    COLLABORATION_RUNTIME_MODULE_TYPE,
    WORKER_ADAPTER_MODULE_TYPE,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    StabilityLevel,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.persistence.artifact_content_store import (
    LocalArtifactContentStore,
)
from sidera_control_plane.persistence.context_package_query_store import (
    SqlAlchemyContextPackageQueryStore,
)
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    PlanningProfileModel,
    WorkerModel,
    WorkerRuntimeConfigurationModel,
)
from sidera_control_plane.persistence.module_repository import (
    capability_profile_from_payload,
)
from sidera_control_plane.persistence.result_input_query_store import (
    SqlAlchemyResultInputQueryStore,
)

_STABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_GOVERNANCE_MIGRATION_HEAD = "0024_r6_3_2_human_release_governance"
_GOVERNANCE_TABLES = frozenset(
    {
        "collaboration_governance_holds",
        "collaboration_release_proposals",
        "collaboration_governance_resolutions",
    }
)
_GOVERNANCE_COMMAND_TYPES = frozenset(
    {
        "PauseCollaborationRelease",
        "ApproveCollaborationReleaseProposal",
        "RejectCollaborationReleaseProposal",
        "ResumeAutomaticCollaboration",
    }
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|provider[_-]?key|token|secret|password|authorization)"
    r"\s*[:=]\s*[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_API_KEY_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    ready: bool
    code: str
    summary: str

    def __post_init__(self) -> None:
        if not _STABLE_NAME.fullmatch(self.name):
            raise ValueError("readiness check names must be stable identifiers")
        if not _STABLE_CODE.fullmatch(self.code):
            raise ValueError("readiness check codes must be stable identifiers")
        if not isinstance(self.ready, bool):
            raise TypeError("readiness state must be boolean")
        object.__setattr__(self, "summary", _safe_summary(self.summary))


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    component: str
    as_of: datetime
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.ready for check in self.checks)

    @property
    def result_input_writes_enabled(self) -> bool:
        check = _check_by_name(self.checks, "result_input_writes")
        return check is not None and check.ready and check.code == "ok"

    @property
    def result_input_capabilities_ready(self) -> bool:
        check = _check_by_name(self.checks, "result_input_capabilities")
        return check is not None and check.ready and check.code == "ok"

    def to_payload(self) -> dict[str, object]:
        return {
            "component": self.component,
            "as_of": self.as_of.isoformat(),
            "status": "ready" if self.ready else "not_ready",
            "result_input_writes_enabled": self.result_input_writes_enabled,
            "result_input_capabilities_ready": self.result_input_capabilities_ready,
            "checks": [
                {
                    "name": check.name,
                    "ready": check.ready,
                    "code": check.code,
                    "summary": check.summary,
                }
                for check in self.checks
            ],
        }


CheckFactory = Callable[[], ReadinessCheck]


class CollaborationReadiness:
    def __init__(self, *, checks: Iterable[CheckFactory]) -> None:
        self._checks = tuple(checks)

    def check(self) -> ReadinessReport:
        results: list[ReadinessCheck] = []
        for factory in self._checks:
            try:
                result = factory()
                if not isinstance(result, ReadinessCheck):
                    raise TypeError("readiness check did not return ReadinessCheck")
            except Exception:  # noqa: BLE001
                result = ReadinessCheck(
                    name="check_execution",
                    ready=False,
                    code="check_failed",
                    summary="readiness check failed",
                )
            results.append(result)
        return ReadinessReport(
            component="sidera-collaboration-worker",
            as_of=datetime.now(UTC),
            checks=tuple(results),
        )


def build_collaboration_readiness(
    settings: Settings,
    database: Database,
    *,
    runtime_factory: CollaborationRuntimeFactory | None = None,
) -> CollaborationReadiness:
    factory = runtime_factory or RegistryCollaborationRuntimeFactory(
        {"reference": lambda _instance: ReferenceCollaborationRuntime()}
    )
    checks: list[CheckFactory] = [
        lambda: _database_check(database),
        lambda: _alembic_check(
            database,
            required_head=(
                _GOVERNANCE_MIGRATION_HEAD
                if settings.collaboration_governance_enabled
                else None
            ),
        ),
        lambda: _reference_runtime_check(factory),
        _capability_check,
        lambda: _result_input_writes_check(settings),
        lambda: _result_input_capabilities_check(settings, database),
    ]
    if settings.collaboration_governance_enabled:
        checks.extend(
            (
                lambda: _governance_schema_check(database),
                _governance_handler_check,
                _collaboration_worker_check,
            )
        )
    else:
        checks.append(_governance_disabled_check)
    return CollaborationReadiness(checks=tuple(checks))


def _database_check(database: Database) -> ReadinessCheck:
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="database",
            ready=False,
            code="database_unavailable",
            summary="database connectivity check failed",
        )
    return ReadinessCheck(
        name="database",
        ready=True,
        code="ok",
        summary="database is reachable",
    )


def _alembic_check(
    database: Database,
    *,
    required_head: str | None = None,
) -> ReadinessCheck:
    try:
        control_plane_root = Path(__file__).resolve().parents[2]
        config = AlembicConfig(str(control_plane_root / "alembic.ini"))
        config.set_main_option(
            "script_location", str(control_plane_root / "migrations")
        )
        heads = set(ScriptDirectory.from_config(config).get_heads())
        with database.engine.connect() as connection:
            current = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
        if (
            not heads
            or current not in heads
            or (required_head is not None and current != required_head)
        ):
            return ReadinessCheck(
                name="alembic",
                ready=False,
                code="alembic_head_mismatch",
                summary="database is not at the expected Alembic head",
            )
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="alembic",
            ready=False,
            code="alembic_unavailable",
            summary="Alembic head could not be verified",
        )
    return ReadinessCheck(
        name="alembic",
        ready=True,
        code="ok",
        summary="database is at the current Alembic head",
    )


def _governance_disabled_check() -> ReadinessCheck:
    return ReadinessCheck(
        name="governance",
        ready=True,
        code="governance_disabled",
        summary=(
            "human release governance is disabled; automatic collaboration remains active"
        ),
    )


def _governance_schema_check(database: Database) -> ReadinessCheck:
    try:
        with database.engine.connect() as connection:
            tables = {
                str(item)
                for item in connection.execute(
                    text(
                        "SELECT table_name "
                        "FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name IN ("
                        "'collaboration_governance_holds', "
                        "'collaboration_release_proposals', "
                        "'collaboration_governance_resolutions')"
                    )
                ).scalars()
            }
            constraint_definition = connection.execute(
                text(
                    "SELECT pg_get_constraintdef(constraint_oid) "
                    "FROM ("
                    "SELECT c.oid AS constraint_oid "
                    "FROM pg_constraint AS c "
                    "JOIN pg_class AS r ON r.oid = c.conrelid "
                    "JOIN pg_namespace AS n ON n.oid = r.relnamespace "
                    "WHERE n.nspname = 'public' "
                    "AND r.relname = 'runtime_intent_applications' "
                    "AND c.conname = 'ck_runtime_intent_applications_status'"
                    ") AS current_constraint"
                )
            ).scalar()
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="governance_schema",
            ready=False,
            code="governance_schema_unavailable",
            summary="governance schema could not be verified",
        )
    if tables != _GOVERNANCE_TABLES:
        return ReadinessCheck(
            name="governance_schema",
            ready=False,
            code="governance_tables_missing",
            summary="the governance ledger tables are not all present",
        )
    if (
        not isinstance(constraint_definition, str)
        or "deferred" not in constraint_definition
    ):
        return ReadinessCheck(
            name="governance_schema",
            ready=False,
            code="governance_deferred_status_missing",
            summary="Runtime Intent Applications do not support deferred status",
        )
    return ReadinessCheck(
        name="governance_schema",
        ready=True,
        code="ok",
        summary="governance ledger tables and deferred status are available",
    )


def _governance_handler_check() -> ReadinessCheck:
    try:
        handler_types = CollaborationGovernanceCommandHandler.command_types
        if handler_types != _GOVERNANCE_COMMAND_TYPES or not callable(
            CollaborationGovernanceCommandHandler.handle
        ):
            raise TypeError("governance command handler contract is incomplete")
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="governance_handler",
            ready=False,
            code="governance_handler_unavailable",
            summary="the four governance commands are not registered",
        )
    return ReadinessCheck(
        name="governance_handler",
        ready=True,
        code="ok",
        summary="Pause, Approve, Reject, and Resume commands are available",
    )


def _collaboration_worker_check() -> ReadinessCheck:
    try:
        from sidera_control_plane.application.collaboration_reconciler import (
            CollaborationReconciler,
        )
        from sidera_control_plane.collaboration_worker import CollaborationWorker

        worker_parameters = inspect.signature(CollaborationWorker).parameters
        reconciler_parameters = inspect.signature(CollaborationReconciler).parameters
        required_worker = {"run_once", "run_forever"}
        if not all(
            callable(getattr(CollaborationWorker, name, None))
            for name in required_worker
        ):
            raise TypeError("collaboration worker methods are incomplete")
        if (
            "recovery_sweep" not in worker_parameters
            or "governance_reconciler" not in reconciler_parameters
        ):
            raise TypeError("collaboration worker has no governance recovery support")
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="collaboration_worker",
            ready=False,
            code="collaboration_worker_incompatible",
            summary="the collaboration worker cannot consume governance state",
        )
    return ReadinessCheck(
        name="collaboration_worker",
        ready=True,
        code="ok",
        summary="the collaboration worker supports governance disposition and recovery",
    )


def _reference_runtime_check(factory: CollaborationRuntimeFactory) -> ReadinessCheck:
    definition, instance = _reference_module_facts()
    try:
        runtime = factory.create(definition, instance)
        if not callable(getattr(runtime, "evaluate", None)):
            raise TypeError("reference runtime does not expose evaluate")
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="reference_runtime",
            ready=False,
            code="reference_runtime_unavailable",
            summary="the exact reference Runtime factory is unavailable",
        )
    return ReadinessCheck(
        name="reference_runtime",
        ready=True,
        code="ok",
        summary="the exact reference Runtime factory is available",
    )


def _capability_check() -> ReadinessCheck:
    capability = _reference_capability()
    required = (
        1 in capability.projection_schema_versions,
        1 in capability.decision_schema_versions,
        "all_required" in capability.strategy_ids,
        capability.idempotency is IdempotencyCapability.OPERATION_ID,
    )
    if not all(required):
        return ReadinessCheck(
            name="capabilities",
            ready=False,
            code="required_capability_missing",
            summary="the reference Runtime does not satisfy required capabilities",
        )
    return ReadinessCheck(
        name="capabilities",
        ready=True,
        code="ok",
        summary="Projection v1, Decision v1, all_required, and operation idempotency are available",
    )


def _result_input_writes_check(settings: Settings) -> ReadinessCheck:
    if not settings.result_inputs_enabled:
        return ReadinessCheck(
            name="result_input_writes",
            ready=False,
            code="result_inputs_disabled",
            summary=(
                "new V5 result-input confirmation writes are disabled; existing V2 readers remain available"
            ),
        )
    return ReadinessCheck(
        name="result_input_writes",
        ready=True,
        code="ok",
        summary="new V5 result-input confirmation writes are explicitly enabled",
    )


def _result_input_capabilities_check(
    settings: Settings, database: Database
) -> ReadinessCheck:
    checks = (
        _result_input_reader_check(database),
        _result_input_runtime_check(),
        _result_input_worker_adapter_check(database),
        _result_input_binding_check(database),
        _result_input_artifact_store_check(settings),
    )
    if not all(check.ready for check in checks):
        return ReadinessCheck(
            name="result_input_capabilities",
            ready=False,
            code="result_input_capability_missing",
            summary=(
                "one or more result-input readers, Runtime, worker adapter, binding, or ArtifactStore capabilities are unavailable"
            ),
        )
    return ReadinessCheck(
        name="result_input_capabilities",
        ready=True,
        code="ok",
        summary=(
            "result-input readers, Runtime 2/2, worker adapter context 2, binding, and ArtifactStore are ready"
        ),
    )


def _result_input_reader_check(database: Database) -> ReadinessCheck:
    required_columns = {
        "collaboration_input_selections": {
            "id",
            "workspace_id",
            "schema_version",
            "groups_json",
            "hard_inputs_json",
            "selection_hash",
        },
        "context_assembly_requests": {
            "manifest_schema_version",
            "input_selection_id",
            "input_selection_hash",
            "result_input_budget",
        },
        "context_packages": {"manifest_schema_version", "result_input_manifest"},
        "context_package_materialization_audits": {
            "package_id",
            "outcome",
            "failure_code",
            "occurred_at",
        },
    }
    try:
        control_plane_root = Path(__file__).resolve().parents[2]
        config = AlembicConfig(str(control_plane_root / "alembic.ini"))
        config.set_main_option(
            "script_location", str(control_plane_root / "migrations")
        )
        directory = ScriptDirectory.from_config(config)
        migration_26 = directory.get_revision("0026_r6_3_4_result_inputs")
        migration_27 = directory.get_revision("0027_r6_3_4_result_input_context")
        if (
            migration_26 is None
            or migration_27 is None
            or migration_27.down_revision != migration_26.revision
        ):
            return ReadinessCheck(
                name="result_input_reader",
                ready=False,
                code="result_input_migration_missing",
                summary="the result-input migration chain is incomplete",
            )
        with database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name, column_name "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name IN ("
                    "'collaboration_input_selections', "
                    "'context_assembly_requests', "
                    "'context_packages', "
                    "'context_package_materialization_audits')"
                )
            ).all()
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="result_input_reader",
            ready=False,
            code="result_input_reader_unavailable",
            summary="result-input repository and V2 context reader could not be verified",
        )

    columns_by_table: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        columns_by_table.setdefault(str(table_name), set()).add(str(column_name))
    if any(
        not required.issubset(columns_by_table.get(table_name, set()))
        for table_name, required in required_columns.items()
    ):
        return ReadinessCheck(
            name="result_input_reader",
            ready=False,
            code="result_input_schema_missing",
            summary="selection and V2 context reader schema is incomplete",
        )
    try:
        reader_methods = (
            SqlAlchemyResultInputQueryStore.load_worker_result_inputs,
            SqlAlchemyResultInputQueryStore.load_input_selection,
            SqlAlchemyContextPackageQueryStore.get_package_for_consumer,
        )
        if not all(callable(method) for method in reader_methods):
            raise TypeError("result-input reader contract is incomplete")
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="result_input_reader",
            ready=False,
            code="result_input_reader_unavailable",
            summary="result-input repository and V2 context reader are unavailable",
        )
    return ReadinessCheck(
        name="result_input_reader",
        ready=True,
        code="ok",
        summary="selection and V2 context readers are available",
    )


def _result_input_runtime_check() -> ReadinessCheck:
    try:
        capability = _reference_capability()
        runtime = ReferenceCollaborationRuntime()
        if not capability.supports_input_groups_v1() or not callable(
            getattr(runtime, "evaluate", None)
        ):
            raise TypeError("result-input Runtime capability is incomplete")
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="result_input_runtime",
            ready=False,
            code="result_input_runtime_capability_missing",
            summary="Reference Runtime does not expose the V2 input-group capability",
        )
    return ReadinessCheck(
        name="result_input_runtime",
        ready=True,
        code="ok",
        summary="Reference Runtime supports Projection 2, Decision 2, and input_groups_v1",
    )


def _result_input_worker_adapter_check(database: Database) -> ReadinessCheck:
    try:
        with database.session() as session:
            rows = session.execute(
                select(
                    WorkerRuntimeConfigurationModel.worker_id,
                    WorkerRuntimeConfigurationModel.adapter_module_config_version,
                    ModuleInstanceModel.config_version,
                    ModuleInstanceModel.lifecycle,
                    ModuleDefinitionModel.capabilities,
                    ModuleDefinitionModel.lifecycle,
                )
                .select_from(WorkerModel)
                .outerjoin(
                    WorkerRuntimeConfigurationModel,
                    and_(
                        WorkerRuntimeConfigurationModel.worker_id == WorkerModel.id,
                        WorkerRuntimeConfigurationModel.workspace_id
                        == WorkerModel.workspace_id,
                    ),
                )
                .outerjoin(
                    ModuleInstanceModel,
                    and_(
                        ModuleInstanceModel.id
                        == WorkerRuntimeConfigurationModel.adapter_module_instance_id,
                        ModuleInstanceModel.workspace_id == WorkerModel.workspace_id,
                    ),
                )
                .outerjoin(
                    ModuleDefinitionModel,
                    and_(
                        ModuleDefinitionModel.id == ModuleInstanceModel.definition_id,
                        ModuleDefinitionModel.workspace_id == WorkerModel.workspace_id,
                    ),
                )
                .where(WorkerModel.desired_state == "active")
            ).all()
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="result_input_worker_adapter",
            ready=False,
            code="result_input_worker_adapter_unavailable",
            summary="selected worker adapter configuration could not be verified",
        )

    for (
        worker_id,
        bound_config_version,
        config_version,
        instance_lifecycle,
        capabilities,
        definition_lifecycle,
    ) in rows:
        del worker_id
        if (
            bound_config_version is None
            or config_version is None
            or instance_lifecycle != "active"
            or definition_lifecycle != "active"
        ):
            return ReadinessCheck(
                name="result_input_worker_adapter",
                ready=False,
                code="result_input_worker_adapter_missing",
                summary="an active worker lacks an active adapter configuration",
            )
        if bound_config_version != config_version:
            return ReadinessCheck(
                name="result_input_worker_adapter",
                ready=False,
                code="result_input_worker_adapter_config_mismatch",
                summary="an active worker adapter binding points to a stale configuration",
            )
        try:
            profile = capability_profile_from_payload(
                WORKER_ADAPTER_MODULE_TYPE, capabilities
            )
        except Exception:  # noqa: BLE001
            return ReadinessCheck(
                name="result_input_worker_adapter",
                ready=False,
                code="result_input_worker_adapter_invalid",
                summary="an active worker adapter capability profile is invalid",
            )
        if not isinstance(profile, WorkerAdapterCapabilityProfile) or not profile.supports_context_package_version(2):
            return ReadinessCheck(
                name="result_input_worker_adapter",
                ready=False,
                code="result_input_worker_context_unsupported",
                summary="an active worker adapter does not support context package version 2",
            )
    return ReadinessCheck(
        name="result_input_worker_adapter",
        ready=True,
        code="ok",
        summary="all active worker adapters support context package version 2",
    )


def _result_input_binding_check(database: Database) -> ReadinessCheck:
    try:
        with database.session() as session:
            profiles = session.scalars(
                select(PlanningProfileModel).where(
                    PlanningProfileModel.status == "active",
                    PlanningProfileModel.user_selectable.is_(True),
                )
            ).all()
            for profile in profiles:
                policy = profile.policy
                if not isinstance(policy, Mapping) or policy.get("proposal_schema_version") != 5:
                    continue
                binding_value = policy.get("collaboration_runtime_binding")
                if not isinstance(binding_value, Mapping):
                    return ReadinessCheck(
                        name="result_input_binding",
                        ready=False,
                        code="result_input_binding_missing",
                        summary="a selectable V5 profile has no Runtime binding",
                    )
                binding = CollaborationRuntimeBindingSnapshot.from_mapping(binding_value)
                instance = session.scalar(
                    select(ModuleInstanceModel).where(
                        ModuleInstanceModel.id == binding.runtime_instance_id,
                        ModuleInstanceModel.workspace_id == profile.workspace_id,
                    )
                )
                definition = session.scalar(
                    select(ModuleDefinitionModel).where(
                        ModuleDefinitionModel.id == binding.runtime_definition_id,
                        ModuleDefinitionModel.workspace_id == profile.workspace_id,
                    )
                )
                if (
                    instance is None
                    or definition is None
                    or instance.definition_id != definition.id
                    or instance.config_version != binding.runtime_config_version
                    or instance.lifecycle != "active"
                    or definition.lifecycle != "active"
                    or not binding.runtime_capability_snapshot.supports_input_groups_v1()
                ):
                    return ReadinessCheck(
                        name="result_input_binding",
                        ready=False,
                        code="result_input_binding_mismatch",
                        summary="a selectable V5 profile Runtime binding is stale or incompatible",
                    )
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="result_input_binding",
            ready=False,
            code="result_input_binding_unavailable",
            summary="selectable V5 profile Runtime bindings could not be verified",
        )
    return ReadinessCheck(
        name="result_input_binding",
        ready=True,
        code="ok",
        summary="selectable V5 profiles have compatible Runtime bindings",
    )


def _result_input_artifact_store_check(settings: Settings) -> ReadinessCheck:
    try:
        root = Path(settings.artifact_root).expanduser().resolve()
        if (
            not root.is_dir()
            or not os.access(root, os.R_OK)
            or settings.artifact_max_bytes <= 0
        ):
            raise OSError("artifact store is not readable")
        store = LocalArtifactContentStore(
            root, max_bytes=settings.artifact_max_bytes
        )
        if not callable(store.read) or not callable(store.content_metadata):
            raise TypeError("artifact store reader contract is incomplete")
    except Exception:  # noqa: BLE001
        return ReadinessCheck(
            name="result_input_artifact_store",
            ready=False,
            code="result_input_artifact_store_unavailable",
            summary="ArtifactStore is not readable with the configured size limit",
        )
    return ReadinessCheck(
        name="result_input_artifact_store",
        ready=True,
        code="ok",
        summary="ArtifactStore can read content within the configured size limit",
    )


def _reference_module_facts() -> tuple[ModuleDefinition, ModuleInstance]:
    definition_id = UUID("f0000000-0000-0000-0000-000000000001")
    definition = ModuleDefinition(
        id=definition_id,
        workspace_id=UUID("f0000000-0000-0000-0000-000000000002"),
        module_type=COLLABORATION_RUNTIME_MODULE_TYPE,
        provider="reference",
        interface_version="collaboration-runtime.v1",
        config_schema_version=1,
        capabilities=_reference_capability(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )
    return definition, ModuleInstance(
        id=UUID("f0000000-0000-0000-0000-000000000003"),
        workspace_id=definition.workspace_id,
        definition_id=definition.id,
        instance_name="sidera-reference",
        config_version=1,
        config_namespace="collaboration.reference",
        config={},
        secret_refs=(),
        lifecycle=ModuleLifecycle.ACTIVE,
    )


def _reference_capability() -> CollaborationRuntimeCapabilityProfile:
    return CollaborationRuntimeCapabilityProfile(
        interface_version="collaboration-runtime.v1",
        projection_schema_versions=(1, 2),
        decision_schema_versions=(1, 2),
        strategy_ids=("all_required", "input_groups_v1"),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def _check_by_name(
    checks: Sequence[ReadinessCheck], name: str
) -> ReadinessCheck | None:
    return next((check for check in checks if check.name == name), None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit SIDERA collaboration worker readiness"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    database: Database | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        database = Database(settings.database_url)
        report = build_collaboration_readiness(settings, database).check()
        if args.as_json:
            print(
                json.dumps(report.to_payload(), sort_keys=True, separators=(",", ":"))
            )
        else:
            print(f"collaboration worker: {'READY' if report.ready else 'NOT READY'}")
            for check in report.checks:
                print(f"{check.name}: {check.code} - {check.summary}")
        return 0 if report.ready else 2
    except Exception:  # noqa: BLE001
        payload = {"status": "error", "error_code": "readiness_check_failed"}
        print(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if args.as_json
            else "collaboration readiness check failed",
            file=sys.stderr,
        )
        return 1
    finally:
        if database is not None:
            database.dispose()


def _safe_summary(value: str) -> str:
    text_value = value or "readiness check failed"
    text_value = _SECRET_ASSIGNMENT.sub("[secret redacted]", text_value)
    text_value = _BEARER_VALUE.sub("[authorization redacted]", text_value)
    text_value = _API_KEY_VALUE.sub("[provider key redacted]", text_value)
    text_value = "".join(
        character
        for character in text_value
        if character.isprintable() or character in {" ", "\t"}
    )
    return " ".join(text_value.split())[:512] or "readiness check failed"


if __name__ == "__main__":
    raise SystemExit(main())
