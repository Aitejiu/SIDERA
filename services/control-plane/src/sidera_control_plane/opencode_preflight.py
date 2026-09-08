import argparse
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sidera_control_plane.adapters.opencode_acp import (
    OpenCodeACPAdapter,
    handoff_profile_supported,
)
from sidera_control_plane.domain.modules import WorkerAdapterCapabilityProfile


@dataclass(frozen=True, slots=True)
class OpenCodePreflightReport:
    status: str
    configured_executable: str
    resolved_executable: str | None
    version: str | None
    workspace: str | None
    capability_profile: WorkerAdapterCapabilityProfile | None
    errors: tuple[str, ...]
    handoff_status: str = "unsupported"

    def as_dict(self) -> dict[str, object]:
        capabilities: dict[str, object]
        if self.capability_profile is None:
            capabilities = {
                "status": "unavailable",
                "reason": self.errors[-1] if self.errors else "probe_failed",
            }
        else:
            profile = self.capability_profile
            capabilities = {
                "status": "available",
                "input": profile.input.value,
                "idempotency": profile.idempotency.value,
                "resume": profile.resume.value,
                "stop": profile.stop.value,
                "sandbox": profile.sandbox.value,
                "stability": profile.stability.value,
            }
        profile = self.capability_profile
        handoff_checkpoint_mode = (
            profile.handoff_checkpoint_mode.value
            if profile is not None
            else "unsupported"
        )
        return {
            "status": self.status,
            "configured_executable": self.configured_executable,
            "resolved_executable": self.resolved_executable,
            "version": self.version,
            "workspace": self.workspace,
            "capabilities": capabilities,
            "handoff_status": self.handoff_status,
            "handoff_checkpoint_mode": handoff_checkpoint_mode,
            "handoff_checkpoint_v1": bool(
                profile is not None and 1 in profile.handoff_checkpoint_versions
            ),
            "handoff_exact_stop": bool(
                profile is not None and handoff_profile_supported(profile)
            ),
            "handoff_session_context_v1": bool(
                profile is not None and 1 in profile.handoff_session_context_versions
            ),
            "handoff_writer_gate": bool(
                profile is not None and 1 in profile.handoff_writer_gate_versions
            ),
            "handoff_inspection": bool(
                profile is not None and profile.handoff_inspection
            ),
            "errors": list(self.errors),
        }


def run_preflight(
    *,
    executable: str,
    git_executable: str = "git",
    expected_version: str | None = None,
    timeout_seconds: float = 30.0,
    context_package_versions: tuple[int, ...] = (1, 2),
) -> OpenCodePreflightReport:
    configured = executable.strip()
    errors: list[str] = []
    if not configured:
        return OpenCodePreflightReport(
            status="failed",
            configured_executable=executable,
            resolved_executable=None,
            version=None,
            workspace=None,
        capability_profile=None,
        errors=("SIDERA_OPENCODE_EXECUTABLE is empty",),
        )
    if timeout_seconds <= 0:
        raise ValueError("OpenCode preflight timeout must be positive")

    resolved = _resolve_executable(configured)
    if resolved is None:
        return OpenCodePreflightReport(
            status="failed",
            configured_executable=configured,
            resolved_executable=None,
            version=None,
            workspace=None,
        capability_profile=None,
        errors=(f"OpenCode executable was not found: {configured}",),
        )

    version = _read_version(resolved, timeout_seconds, errors)
    expected = expected_version.strip() if expected_version else None
    if expected and (version is None or expected not in version):
        errors.append(
            f"OpenCode version does not contain the pinned value {expected!r}: "
            f"{version or 'unavailable'}"
        )

    profile: WorkerAdapterCapabilityProfile | None = None
    workspace: str | None = None
    try:
        with _temporary_git_worktree(git_executable) as worktree:
            workspace = str(worktree)
            profile = OpenCodeACPAdapter(
                executable=resolved,
                request_timeout_seconds=timeout_seconds,
                context_package_versions=context_package_versions,
            ).probe({"cwd": workspace})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ACP initialize/session-new probe failed: {_error_text(exc)}")

    return OpenCodePreflightReport(
        status="ok" if not errors and profile is not None else "failed",
        configured_executable=configured,
        resolved_executable=resolved,
        version=version,
        workspace=workspace,
        capability_profile=profile,
        errors=tuple(errors),
        handoff_status=(
            "supported"
            if profile is not None and handoff_profile_supported(profile)
            else "unsupported"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the configured OpenCode executable and ACP contract"
    )
    parser.parse_args(argv)
    configured = os.environ.get("SIDERA_OPENCODE_EXECUTABLE", "opencode")
    git_executable = os.environ.get("SIDERA_GIT_EXECUTABLE", "git")
    expected = os.environ.get("SIDERA_OPENCODE_EXPECTED_VERSION")
    try:
        timeout = float(
            os.environ.get("SIDERA_OPENCODE_PROBE_TIMEOUT_SECONDS", "30")
        )
        report = run_preflight(
            executable=configured,
            git_executable=git_executable,
            expected_version=expected,
            timeout_seconds=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        report = OpenCodePreflightReport(
            status="failed",
            configured_executable=configured,
            resolved_executable=None,
            version=None,
            workspace=None,
            capability_profile=None,
            errors=(f"preflight configuration failed: {_error_text(exc)}",),
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
        errors.append(f"OpenCode version command failed: {_error_text(exc)}")
        return None
    output = (result.stdout or result.stderr).strip()
    version = output.splitlines()[0][:200] if output else None
    if result.returncode != 0:
        errors.append(
            f"OpenCode version command exited {result.returncode}: "
            f"{version or 'no output'}"
        )
    return version


class _TemporaryGitWorktree:
    def __init__(self, git_executable: str) -> None:
        self._git_executable = git_executable.strip()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> Path:
        if not self._git_executable:
            raise ValueError("git executable is required for OpenCode preflight")
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="sidera-opencode-preflight-"
        )
        root = Path(self._temporary_directory.name)
        repository = root / "repository"
        worktree = root / "worktree"
        repository.mkdir()
        _git(self._git_executable, ["init", "--initial-branch=main", str(repository)])
        _git(
            self._git_executable,
            ["-C", str(repository), "config", "user.name", "SIDERA Preflight"],
        )
        _git(
            self._git_executable,
            ["-C", str(repository), "config", "user.email", "preflight@sidera.local"],
        )
        (repository / "README.md").write_text("SIDERA OpenCode preflight\n")
        (repository / "SIDERA_PREFLIGHT_SENTINEL.txt").write_text(
            "SIDERA preflight sentinel\n"
        )
        _git(
            self._git_executable,
            ["-C", str(repository), "add", "README.md", "SIDERA_PREFLIGHT_SENTINEL.txt"],
        )
        _git(
            self._git_executable,
            ["-C", str(repository), "commit", "-m", "preflight base"],
        )
        _git(
            self._git_executable,
            [
                "-C",
                str(repository),
                "worktree",
                "add",
                "-b",
                "sidera/preflight",
                str(worktree),
                "HEAD",
            ],
        )
        self.path = worktree
        return worktree

    def __exit__(self, *_: object) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()


def _temporary_git_worktree(git_executable: str) -> _TemporaryGitWorktree:
    return _TemporaryGitWorktree(git_executable)


def _git(git_executable: str, arguments: list[str]) -> None:
    try:
        result = subprocess.run(
            [git_executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Git command failed to start: {_error_text(exc)}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise RuntimeError(f"Git command exited {result.returncode}: {detail[:500]}")


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


if __name__ == "__main__":
    raise SystemExit(main())
