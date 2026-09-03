"""Seed the deterministic R6.3.2 human-release governance vertical fixture."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sidera_control_plane.persistence.database import Database
from tests.e2e.seed_r6_3_1_collaboration import (
    NOW,
    CollaborationSeed,
    seed_collaboration_plan,
)


def seed_governance_plan(
    database: Database,
    workspace_id: UUID,
    *,
    now=NOW,
    reset: bool = False,
) -> CollaborationSeed:
    """Reuse the fan-in graph with two eligible siblings and one downstream gate."""

    return seed_collaboration_plan(
        database,
        workspace_id,
        now=now,
        reset=reset,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("SIDERA_ALLOW_E2E_SEED") != "1":
        raise SystemExit("SIDERA_ALLOW_E2E_SEED=1 is required for the R6.3.2 seed")
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if not database_url:
        raise SystemExit("SIDERA_DATABASE_URL is required for the R6.3.2 seed")
    if urlsplit(database_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("R6.3.2 E2E seed requires a loopback database host")
    try:
        workspace_id = UUID(args.workspace_id)
    except ValueError as exc:
        raise SystemExit("--workspace-id must be a UUID") from exc

    database = Database(database_url)
    try:
        seed = seed_governance_plan(database, workspace_id, reset=args.reset)
    finally:
        database.dispose()
    print(json.dumps(seed.to_mapping(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
