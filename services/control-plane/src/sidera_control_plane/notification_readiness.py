from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import and_, func, select, text

from sidera_control_plane.application.notification_policy import POLICY_VERSION
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    ModuleDefinitionModel,
    ModuleInstanceModel,
    NotificationDeliveryIntentModel,
    NotificationReconciliationCursorModel,
    NotificationRouteModel,
    ProjectionChangeModel,
    WorkspaceProjectionCursorModel,
)

_NOTIFICATION_MIGRATION_HEAD = "0025_r6_3_3_notification_gateway"
_EXTERNAL_ROUTE_STATUSES = ("active", "draining")


@dataclass(frozen=True, slots=True)
class NotificationReadinessCheck:
    name: str
    ready: bool
    code: str
    summary: str


@dataclass(frozen=True, slots=True)
class NotificationReadinessReport:
    checks: tuple[NotificationReadinessCheck, ...]
    as_of: datetime

    @property
    def ready(self) -> bool:
        return all(check.ready for check in self.checks)

    def to_payload(self) -> dict[str, object]:
        return {
            "component": "sidera-notification-worker",
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


def evaluate_notification_readiness(
    *,
    migration_ready: bool,
    notification_enabled: bool,
    policy_version: int,
    baseline_required: bool,
    baseline_complete: bool,
    cursor_lag: int,
    active_external_routes: int,
    cursor_last_error: str | None = None,
    unavailable_channels: int = 0,
    incompatible_channels: int = 0,
    delivery_backlog: int = 0,
    exhausted_deliveries: int = 0,
    as_of: datetime | None = None,
) -> NotificationReadinessReport:
    """Evaluate the notification worker's operational gates.

    The function is intentionally pure so callers can use the same fixed codes
    for a database-backed readiness endpoint, a CLI, and tests.
    """
    if cursor_lag < 0:
        raise ValueError("cursor_lag must be non-negative")
    if active_external_routes < 0:
        raise ValueError("active_external_routes must be non-negative")
    for name, value in (
        ("unavailable_channels", unavailable_channels),
        ("incompatible_channels", incompatible_channels),
        ("delivery_backlog", delivery_backlog),
        ("exhausted_deliveries", exhausted_deliveries),
    ):
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
    stable_cursor_error = _stable_readiness_error(cursor_last_error)
    checks = (
        NotificationReadinessCheck(
            name="migration",
            ready=migration_ready,
            code=(
                "notification_migration_ready"
                if migration_ready
                else "notification_migration_unready"
            ),
            summary=(
                "notification schema is at the required migration"
                if migration_ready
                else "notification schema is not at the required migration"
            ),
        ),
        NotificationReadinessCheck(
            name="policy",
            ready=notification_enabled and policy_version == POLICY_VERSION,
            code=(
                "notification_disabled"
                if not notification_enabled
                else (
                    "notification_policy_ready"
                    if policy_version == POLICY_VERSION
                    else "notification_policy_unsupported"
                )
            ),
            summary=(
                "notification worker is disabled"
                if not notification_enabled
                else (
                    "notification policy is supported and enabled"
                    if policy_version == POLICY_VERSION
                    else "notification policy version is unsupported"
                )
            ),
        ),
        NotificationReadinessCheck(
            name="baseline",
            ready=not baseline_required or baseline_complete,
            code=(
                "notification_baseline_ready"
                if not baseline_required or baseline_complete
                else "notification_baseline_required"
            ),
            summary=(
                "current-state baseline is complete"
                if baseline_complete
                else (
                    "current-state baseline is optional"
                    if not baseline_required
                    else "current-state baseline must be completed"
                )
            ),
        ),
        NotificationReadinessCheck(
            name="cursor",
            ready=cursor_lag == 0 and cursor_last_error is None,
            code=(
                "notification_cursor_error"
                if stable_cursor_error is not None
                else (
                    "notification_cursor_caught_up"
                    if cursor_lag == 0
                    else "notification_cursor_lagging"
                )
            ),
            summary=(
                f"notification reconciliation reported a stable error: "
                f"{stable_cursor_error}"
                if stable_cursor_error is not None
                else (
                    "notification reconciliation cursor is caught up"
                    if cursor_lag == 0
                    else "notification reconciliation cursor is behind"
                )
            ),
        ),
        NotificationReadinessCheck(
            name="channel",
            ready=unavailable_channels == 0 and incompatible_channels == 0,
            code=(
                "notification_channel_ready"
                if unavailable_channels == 0 and incompatible_channels == 0
                else "notification_channel_degraded"
            ),
            summary=(
                "notification channel module lifecycle and capabilities are ready"
                if unavailable_channels == 0 and incompatible_channels == 0
                else "one or more notification channel modules are unavailable"
            ),
        ),
        NotificationReadinessCheck(
            name="delivery",
            ready=exhausted_deliveries == 0,
            code=(
                "notification_delivery_exhausted"
                if exhausted_deliveries > 0
                else (
                    "notification_delivery_backlog"
                    if delivery_backlog > 0
                    else (
                        "web_only_ready"
                        if active_external_routes == 0
                        else "notification_external_route_configured"
                    )
                )
            ),
            summary=(
                "notification delivery has exhausted items"
                if exhausted_deliveries > 0
                else (
                    "notification delivery backlog is present"
                    if delivery_backlog > 0
                    else (
                        "web-only notification operation is available"
                        if active_external_routes == 0
                        else "external notification channel configuration is present"
                    )
                )
            ),
        ),
    )
    return NotificationReadinessReport(
        checks=checks,
        as_of=as_of or datetime.now(UTC),
    )


def build_notification_readiness(
    settings: Settings,
    database: Database,
    *,
    workspace_id: UUID | None = None,
) -> NotificationReadinessReport:
    now = datetime.now(UTC)
    migration_ready = _migration_ready(database)
    baseline_complete = _baseline_complete(database, workspace_id)
    cursor_lag = _cursor_lag(database, workspace_id)
    active_external_routes = _active_external_routes(database, workspace_id)
    unavailable_channels, incompatible_channels = _channel_health(
        database, workspace_id
    )
    delivery_backlog, exhausted_deliveries = _delivery_state(database, workspace_id)
    return evaluate_notification_readiness(
        migration_ready=migration_ready,
        notification_enabled=settings.notification_enabled,
        policy_version=settings.notification_policy_version,
        baseline_required=settings.notification_baseline_required,
        baseline_complete=baseline_complete,
        cursor_lag=cursor_lag,
        active_external_routes=active_external_routes,
        unavailable_channels=unavailable_channels,
        incompatible_channels=incompatible_channels,
        delivery_backlog=delivery_backlog,
        exhausted_deliveries=exhausted_deliveries,
        as_of=now,
    )


def _migration_ready(database: Database) -> bool:
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
        return bool(heads) and current in heads and current == _NOTIFICATION_MIGRATION_HEAD
    except Exception:  # noqa: BLE001 - readiness fails closed
        return False


def _baseline_complete(database: Database, workspace_id: UUID | None) -> bool:
    if workspace_id is None:
        return False
    try:
        with database.session() as session:
            return (
                session.scalar(
                    select(NotificationReconciliationCursorModel.workspace_id).where(
                        NotificationReconciliationCursorModel.workspace_id
                        == workspace_id,
                        NotificationReconciliationCursorModel.policy_version
                        == POLICY_VERSION,
                    )
                )
                is not None
            )
    except Exception:  # noqa: BLE001 - readiness fails closed
        return False


def _cursor_lag(database: Database, workspace_id: UUID | None) -> int:
    if workspace_id is None:
        return 0
    try:
        with database.session() as session:
            latest_projection = int(
                session.scalar(
                    select(func.max(ProjectionChangeModel.cursor)).where(
                        ProjectionChangeModel.workspace_id == workspace_id
                    )
                )
                or 0
            )
            latest_workspace_cursor = int(
                session.scalar(
                    select(WorkspaceProjectionCursorModel.last_cursor).where(
                        WorkspaceProjectionCursorModel.workspace_id == workspace_id
                    )
                )
                or 0
            )
            last = int(
                session.scalar(
                    select(NotificationReconciliationCursorModel.last_projection_cursor).where(
                        NotificationReconciliationCursorModel.workspace_id
                        == workspace_id,
                        NotificationReconciliationCursorModel.policy_version
                        == POLICY_VERSION,
                    )
                )
                or 0
            )
        return max(0, max(latest_projection, latest_workspace_cursor) - last)
    except Exception:  # noqa: BLE001 - readiness fails closed
        return 1


def _active_external_routes(database: Database, workspace_id: UUID | None) -> int:
    if workspace_id is None:
        return 0
    try:
        with database.session() as session:
            return int(
                session.scalar(
                    select(func.count(NotificationRouteModel.id)).where(
                        NotificationRouteModel.workspace_id == workspace_id,
                        NotificationRouteModel.status.in_(_EXTERNAL_ROUTE_STATUSES),
                        NotificationRouteModel.channel_type != "web",
                    )
                )
                or 0
            )
    except Exception:  # noqa: BLE001 - readiness fails closed
        return 1


def _channel_health(database: Database, workspace_id: UUID | None) -> tuple[int, int]:
    if workspace_id is None:
        return 0, 0
    unavailable = 0
    incompatible = 0
    try:
        with database.session() as session:
            rows = session.execute(
                select(
                    NotificationRouteModel.channel_type,
                    NotificationRouteModel.status,
                    ModuleDefinitionModel.lifecycle,
                    ModuleInstanceModel.lifecycle,
                    ModuleDefinitionModel.capabilities,
                )
                .join(
                    ModuleInstanceModel,
                    and_(
                        ModuleInstanceModel.id
                        == NotificationRouteModel.channel_module_instance_id,
                        ModuleInstanceModel.workspace_id
                        == NotificationRouteModel.workspace_id,
                    ),
                )
                .join(
                    ModuleDefinitionModel,
                    and_(
                        ModuleDefinitionModel.id == ModuleInstanceModel.definition_id,
                        ModuleDefinitionModel.workspace_id
                        == ModuleInstanceModel.workspace_id,
                    ),
                )
                .where(
                    NotificationRouteModel.workspace_id == workspace_id,
                    NotificationRouteModel.status.in_(_EXTERNAL_ROUTE_STATUSES),
                    NotificationRouteModel.channel_type != "web",
                )
            ).all()
        for channel_type, _route_status, definition_lifecycle, instance_lifecycle, capabilities in rows:
            if definition_lifecycle != "active" or instance_lifecycle != "active":
                unavailable += 1
                continue
            if not isinstance(capabilities, dict):
                incompatible += 1
                continue
            channel_types = capabilities.get("channel_types")
            contract_versions = capabilities.get("delivery_contract_versions")
            if (
                not isinstance(channel_types, list)
                or channel_type not in channel_types
                or not isinstance(contract_versions, list)
                or 1 not in contract_versions
            ):
                incompatible += 1
        return unavailable, incompatible
    except Exception:  # noqa: BLE001 - readiness fails closed
        return 1, 0


def _delivery_state(database: Database, workspace_id: UUID | None) -> tuple[int, int]:
    if workspace_id is None:
        return 0, 0
    try:
        with database.session() as session:
            backlog = int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationDeliveryIntentModel)
                    .where(
                        NotificationDeliveryIntentModel.workspace_id == workspace_id,
                        NotificationDeliveryIntentModel.status.in_(
                            ("pending", "claimed", "retry_wait")
                        ),
                    )
                )
                or 0
            )
            exhausted = int(
                session.scalar(
                    select(func.count())
                    .select_from(NotificationDeliveryIntentModel)
                    .where(
                        NotificationDeliveryIntentModel.workspace_id == workspace_id,
                        NotificationDeliveryIntentModel.status == "exhausted",
                    )
                )
                or 0
            )
        return backlog, exhausted
    except Exception:  # noqa: BLE001 - readiness fails closed
        return 1, 1


def _stable_readiness_error(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.lower() if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or not value
        or any(marker in lowered for marker in ("token", "secret", "password"))
        or any(not (character.isalnum() or character in "_.:-") for character in value)
        or not value[0].isalpha()
    ):
        return "unknown"
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit SIDERA notification worker readiness"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--workspace-id")
    args = parser.parse_args(argv)
    database: Database | None = None
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
        workspace_id = UUID(args.workspace_id) if args.workspace_id else None
        database = Database(settings.database_url)
        report = build_notification_readiness(
            settings, database, workspace_id=workspace_id
        )
        if args.as_json:
            print(
                json.dumps(report.to_payload(), sort_keys=True, separators=(",", ":"))
            )
        else:
            print(f"notification worker: {'READY' if report.ready else 'NOT READY'}")
            for check in report.checks:
                print(f"{check.name}: {check.code} - {check.summary}")
        return 0 if report.ready else 2
    except Exception:  # noqa: BLE001 - stable CLI failure output
        payload = {
            "component": "sidera-notification-worker",
            "status": "error",
            "error_code": "notification_readiness_check_failed",
        }
        print(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if args.as_json
            else "notification readiness check failed",
            file=sys.stderr,
        )
        return 1
    finally:
        if database is not None:
            database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
