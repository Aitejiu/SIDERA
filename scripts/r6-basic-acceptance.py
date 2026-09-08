#!/usr/bin/env python3
"""Run one bounded R6 basic-acceptance scenario without implicit provider calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1] / "services" / "control-plane"
sys.path.insert(0, str(CONTROL_PLANE_ROOT))
from tests.e2e.basic_acceptance.runner import run_scenario  # pyright: ignore[reportMissingImports]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", choices=("check", "local", "real"), default="check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.manifest.exists():
        print(json.dumps({"status": "INCOMPLETE", "failure_code": "manifest_missing"}))
        return 2
    try:
        evidence = run_scenario(args.scenario, args.manifest, mode=args.mode)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        print(json.dumps({"status": "INCOMPLETE", "failure_code": "manifest_invalid"}))
        return 2
    print(json.dumps(evidence.model_dump(mode="json"), sort_keys=True))
    if evidence.status == "FAIL":
        return 1
    if evidence.status == "INCOMPLETE":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
