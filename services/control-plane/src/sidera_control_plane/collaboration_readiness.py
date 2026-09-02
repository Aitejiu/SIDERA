from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text

from sidera_control_plane.adapters.reference_collaboration_runtime import (
    ReferenceCollaborationRuntime,
)
from sidera_control_plane.application.collaboration_runtime_resolver import (
    CollaborationRuntimeFactory,
    RegistryCollaborationRuntimeFactory,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.domain.modules import (
    COLLABORATION_RUNTIME_MODULE_TYPE,
    CollaborationRuntimeCapabilityProfile,
    IdempotencyCapability,
    ModuleDefinition,
    ModuleInstance,
    ModuleLifecycle,
    StabilityLevel,
)
from sidera_control_plane.persistence.database import Database

_STABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
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

    def to_payload(self) -> dict[str, object]:
        return {
            "component": self.component,
            "as_of": self.as_of.isoformat(),
            "status": "ready" if self.ready else "not_ready",
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
    return CollaborationReadiness(
        checks=(
            lambda: _database_check(database),
            lambda: _alembic_check(database),
            lambda: _reference_runtime_check(factory),
            lambda: _capability_check(),
        )
    )


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


def _alembic_check(database: Database) -> ReadinessCheck:
    try:
        control_plane_root = Path(__file__).resolve().parents[2]
        config = AlembicConfig(str(control_plane_root / "alembic.ini"))
        config.set_main_option(
            "script_location", str(control_plane_root / "migrations")
        )
        heads = set(ScriptDirectory.from_config(config).get_heads())
        with database.engine.connect() as connection:
            current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if not heads or current not in heads:
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
        projection_schema_versions=(1,),
        decision_schema_versions=(1,),
        strategy_ids=("all_required",),
        idempotency=IdempotencyCapability.OPERATION_ID,
        max_projection_bytes=262_144,
        max_decision_bytes=65_536,
        max_intents_per_decision=128,
        max_strategy_state_bytes=16_384,
        stability=StabilityLevel.STABLE,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit SIDERA collaboration worker readiness")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    database: Database | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        database = Database(settings.database_url)
        report = build_collaboration_readiness(settings, database).check()
        if args.as_json:
            print(json.dumps(report.to_payload(), sort_keys=True, separators=(",", ":")))
        else:
            print(
                f"collaboration worker: {'READY' if report.ready else 'NOT READY'}"
            )
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
