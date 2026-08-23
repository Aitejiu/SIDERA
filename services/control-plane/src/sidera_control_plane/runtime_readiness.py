from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from sidera_control_plane.application.runtime_metrics import RuntimeReadinessView
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.runtime_metrics_store import (
    SqlAlchemyRuntimeMetricsStore,
)


def audit_readiness(database: Database, workspace_id: UUID) -> RuntimeReadinessView:
    return SqlAlchemyRuntimeMetricsStore(database).get_readiness(workspace_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit SIDERA runtime readiness")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        workspace_id = UUID(args.workspace_id)
        settings = Settings()  # pyright: ignore[reportCallIssue]
        database = Database(settings.database_url)
    except Exception:  # noqa: BLE001
        _print_error(args.as_json)
        return 1

    try:
        report = audit_readiness(database, workspace_id)
        if args.as_json:
            print(json.dumps(report.to_payload(), sort_keys=True, separators=(",", ":")))
        else:
            _print_human(report)
        return 0 if report.ready else 2
    except Exception:  # noqa: BLE001
        _print_error(args.as_json)
        return 1
    finally:
        database.dispose()


def _print_human(report: RuntimeReadinessView) -> None:
    status = "READY" if report.ready else "ACTION REQUIRED"
    print(f"workspace {report.workspace_id}: {status}")
    print(f"active workers: {len(report.workers)}")
    print(f"missing runtime configuration: {report.missing_runtime_configuration}")
    print(f"invalid runtime configuration: {report.invalid_runtime_configuration}")
    print(f"executions without binding: {len(report.executions_without_binding)}")
    print(f"module mismatches: {len(report.module_mismatches)}")


def _print_error(as_json: bool) -> None:
    payload = {"status": "error", "error_code": "readiness_audit_failed"}
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)
    else:
        print("readiness audit failed", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
