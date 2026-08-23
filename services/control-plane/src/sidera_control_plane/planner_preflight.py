from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sidera_control_plane.adapters.opencode_planner import OpenCodePlannerAdapter
from sidera_control_plane.domain.modules import PlannerAdapterCapabilityProfile


@dataclass(frozen=True, slots=True)
class PlannerPreflightReport:
    status: str
    configured_executable: str
    resolved_executable: str | None
    version: str | None
    capabilities: dict[str, object]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "configured_executable": self.configured_executable,
            "resolved_executable": self.resolved_executable,
            "version": self.version,
            "capabilities": dict(self.capabilities),
            "errors": list(self.errors),
        }


def run_planner_preflight(
    *,
    executable: str,
    expected_version: str | None = None,
    temp_root: Path | None = None,
    timeout_seconds: float = 30.0,
) -> PlannerPreflightReport:
    configured = executable.strip()
    errors: list[str] = []
    unavailable: dict[str, object] = {"status": "unavailable"}
    if not configured:
        return PlannerPreflightReport(
            status="failed",
            configured_executable=executable,
            resolved_executable=None,
            version=None,
            capabilities=unavailable,
            errors=("planner executable is empty",),
        )
    if timeout_seconds <= 0:
        raise ValueError("planner preflight timeout must be positive")

    resolved = _resolve_executable(configured)
    if resolved is None:
        return PlannerPreflightReport(
            status="failed",
            configured_executable=configured,
            resolved_executable=None,
            version=None,
            capabilities=unavailable,
            errors=(f"planner executable was not found: {configured}",),
        )

    version = _read_version(resolved, timeout_seconds, errors)
    expected = expected_version.strip() if expected_version else None
    if expected and (version is None or expected not in version):
        errors.append(
            f"planner version does not contain the pinned value {expected!r}"
        )

    capability_profile: PlannerAdapterCapabilityProfile | None = None
    try:
        parent = (temp_root or Path.cwd()).expanduser().resolve().parent
        with tempfile.TemporaryDirectory(prefix="sidera-planner-preflight-", dir=parent) as directory:
            adapter = OpenCodePlannerAdapter(
                executable=resolved,
                temp_root=temp_root or Path(directory) / "runs",
                request_timeout_seconds=timeout_seconds,
            )
            capability_profile = adapter.probe({"cwd": directory})
            adapter.validate_schema_round_trip()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"planner ACP probe failed: {type(exc).__name__}")

    return PlannerPreflightReport(
        status="ok" if not errors and capability_profile is not None else "failed",
        configured_executable=configured,
        resolved_executable=resolved,
        version=version,
        capabilities=(
            _capabilities(capability_profile)
            if capability_profile is not None
            else unavailable
        ),
        errors=tuple(errors),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the isolated SIDERA OpenCode planner contract"
    )
    parser.parse_args(argv)
    configured = os.environ.get(
        "SIDERA_PLANNING_OPENCODE_EXECUTABLE",
        os.environ.get("SIDERA_OPENCODE_EXECUTABLE", "opencode"),
    )
    expected = os.environ.get("SIDERA_PLANNING_OPENCODE_EXPECTED_VERSION")
    timeout_text = os.environ.get(
        "SIDERA_PLANNING_OPENCODE_PROBE_TIMEOUT_SECONDS", "30"
    )
    try:
        timeout = float(timeout_text)
        report = run_planner_preflight(
            executable=configured,
            expected_version=expected,
            timeout_seconds=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        report = PlannerPreflightReport(
            status="failed",
            configured_executable=configured,
            resolved_executable=None,
            version=None,
            capabilities={"status": "unavailable"},
            errors=(f"preflight configuration failed: {type(exc).__name__}",),
        )
    print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
    return 0 if report.status == "ok" else 1


def _resolve_executable(configured: str) -> str | None:
    candidate = Path(configured).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate.resolve())
        return None
    return shutil.which(configured)


def _read_version(
    executable: str, timeout_seconds: float, errors: list[str]
) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"planner version command failed: {type(exc).__name__}")
        return None
    output = (result.stdout or result.stderr).strip()
    version = output.splitlines()[0][:200] if output else None
    if result.returncode != 0:
        errors.append(f"planner version command exited {result.returncode}")
    return version


def _capabilities(profile: PlannerAdapterCapabilityProfile) -> dict[str, object]:
    return {
        "status": "available",
        "schema_round_trip": "ok",
        "structured_output": profile.structured_output.value,
        "clarification": profile.clarification,
        "idempotency": profile.idempotency.value,
        "cancellation": profile.cancellation.value,
        "usage_reporting": profile.usage_reporting.value,
        "max_context_bytes": profile.max_context_bytes,
        "stability": profile.stability.value,
        "interface_version": profile.interface_version,
        "proposal_schema_versions": list(profile.proposal_schema_versions),
    }


if __name__ == "__main__":
    raise SystemExit(main())
