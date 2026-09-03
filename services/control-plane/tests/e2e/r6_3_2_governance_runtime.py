"""Advance the R6.3.2 deterministic governance fixture after a Main Agent action."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sidera_control_plane.persistence.database import Database
from tests.e2e.r6_3_1_fake_runtime import CollaborationScenario, _seed_for_workspace
from tests.e2e.seed_r6_3_2_governance import NOW


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--start-sibling",
        action="store_true",
        help="start the upstream-A session used by the browser gate",
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.3.2 runtime")
    if urlsplit(database_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("R6.3.2 runtime requires a loopback database host")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc

    database = Database(database_url)
    try:
        seed = _seed_for_workspace(workspace_id)
        scenario = CollaborationScenario(database, seed, now=NOW)
        if args.start_sibling:
            scenario.run_collaboration_until_idle()
            scenario.activation.run_once(limit=20)
            scenario.scheduler.run_once(limit=20)
            execution_id, session_id = scenario._start_browser_session("upstream-a")
            result = {
                "run_status": scenario.run_status(),
                "execution_id": str(execution_id),
                "session_id": str(session_id),
            }
        else:
            scenario.succeed("upstream-a")
            scenario.run_collaboration_until_idle()
            scenario.succeed("upstream-b", evidence=("verification-report",))
            scenario.run_collaboration_until_idle()
            result = {"run_status": scenario.run_status()}
    finally:
        database.dispose()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
