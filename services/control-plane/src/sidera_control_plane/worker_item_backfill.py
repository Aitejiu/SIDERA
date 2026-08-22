from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from sidera_control_plane.application.worker_item_backfill import (
    RootBackfillReport,
    WorkerItemBackfillService,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.plan_repository import PlanRepository
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _report_payload(report: RootBackfillReport) -> dict[str, object]:
    return {
        "root_task_id": str(report.root_task_id),
        "created_worker_items": report.created_worker_items,
        "created_placements": report.created_placements,
        "linked_executions": report.linked_executions,
        "ambiguous_execution_ids": [
            str(execution_id) for execution_id in report.ambiguous_execution_ids
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill historical v1 plan snapshots into WorkerItems."
    )
    parser.add_argument(
        "--workspace-id",
        required=True,
        type=UUID,
        help="Workspace whose historical plan snapshots should be backfilled.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()  # pyright: ignore[reportCallIssue]
    database = Database(settings.database_url)
    service = WorkerItemBackfillService()
    failures = 0
    try:
        with database.session() as session:
            root_task_ids = PlanRepository(session).list_roots_with_snapshots(
                args.workspace_id
            )
        for root_task_id in root_task_ids:
            try:
                with SqlAlchemyUnitOfWork(database) as uow:
                    report = service.backfill_root(
                        uow, args.workspace_id, root_task_id
                    )
                print(json.dumps(_report_payload(report), sort_keys=True))
            except Exception as exc:  # noqa: BLE001 - isolate one root transaction
                failures += 1
                print(
                    json.dumps(
                        {
                            "root_task_id": str(root_task_id),
                            "error": str(exc),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
    finally:
        database.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
