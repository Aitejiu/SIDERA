"""Print the effective, secret-free R6 acceptance profile inspection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sidera_control_plane.basic_acceptance_config import (
    inspect_profiles,
    prepare_process_environment,
    write_process_environment,
)
from sidera_control_plane.basic_acceptance_manifest import (
    load_manifest,
    validate_manifest_isolation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--environment-file", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest_isolation(
            manifest,
            sidera_root=Path(__file__).resolve().parents[1],
        )
        report = inspect_profiles(manifest, environ=os.environ)
        if args.environment_file is not None:
            write_process_environment(
                args.environment_file,
                prepare_process_environment(manifest, environ=os.environ),
            )
    except (OSError, ValueError) as exc:
        payload = {"status": "unavailable", "error_code": "manifest_invalid"}
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        print(f"effective config inspection failed: {exc}", file=sys.stderr)
        return 2

    payload = report.model_dump(mode="json")
    payload["status"] = (
        "verified"
        if all(profile.status == "verified" for profile in report.resolved_profiles)
        else "unavailable"
    )
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if args.as_json else None,
        )
    )
    return 0 if payload["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
