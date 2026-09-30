#!/usr/bin/env python3
"""Run one bounded R6 basic-acceptance scenario without implicit provider calls."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1] / "services" / "control-plane"
SIDERA_ROOT = CONTROL_PLANE_ROOT.parent.parent
sys.path.insert(0, str(CONTROL_PLANE_ROOT))
from sidera_runtime_guard import (  # pyright: ignore[reportMissingImports]
    RuntimeBudgetGuard,
    RuntimeGuardError,
    prepare_run,
)

from sidera_control_plane.basic_acceptance_manifest import (  # pyright: ignore[reportMissingImports]
    load_manifest,
    validate_manifest_isolation,
)
from tests.e2e.basic_acceptance.runner import (  # pyright: ignore[reportMissingImports]
    SCENARIOS,
    run_scenario,
)


class RealPreflightError(ValueError):
    """A stable, secret-free reason why a real run must not start."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def preflight_real_manifest(manifest_path: Path, scenario: str) -> dict[str, object]:
    """Validate the real-run boundary before any scenario observation occurs."""

    if os.environ.get("SIDERA_RUN_REAL_BASIC") != "1":
        raise RealPreflightError("real_mode_not_authorized")
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RealPreflightError("manifest_invalid") from exc
    if manifest.schema_version != 2:
        raise RealPreflightError("real_manifest_schema_required")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RealPreflightError("manifest_invalid") from exc
    if raw_manifest.get("resource_binding_mode") != "persisted":
        raise RealPreflightError("real_resource_binding_mode_required")
    if scenario not in SCENARIOS or scenario not in manifest.scenarios:
        raise RealPreflightError("real_scenario_not_declared")
    try:
        validate_manifest_isolation(manifest, sidera_root=SIDERA_ROOT)
        prepared = prepare_run(manifest_path)
        guard = RuntimeBudgetGuard(manifest_path)
    except RuntimeGuardError as exc:
        raise RealPreflightError(exc.code) from exc
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RealPreflightError("real_preflight_invalid") from exc
    if guard.run_id != str(manifest.run_id) or guard.workspace_id != str(
        manifest.workspace_id
    ):
        raise RealPreflightError("real_guard_identity_mismatch")
    return prepared


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--scenario")
    mode_group.add_argument("--prepare-run", action="store_true")
    parser.add_argument("--mode", choices=("check", "local", "real"), default="check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.manifest.exists():
        print(json.dumps({"status": "INCOMPLETE", "failure_code": "manifest_missing"}))
        return 2
    try:
        if args.prepare_run:
            if args.mode == "real":
                print(
                    json.dumps(
                        {
                            "status": "INCOMPLETE",
                            "failure_code": "prepare_real_mode_conflict",
                        }
                    )
                )
                return 2
            result = prepare_run(args.manifest)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.mode == "real":
            preflight_real_manifest(args.manifest, args.scenario)
        evidence = run_scenario(args.scenario, args.manifest, mode=args.mode)
    except RealPreflightError as exc:
        print(json.dumps({"status": "INCOMPLETE", "failure_code": exc.code}))
        return 2
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
