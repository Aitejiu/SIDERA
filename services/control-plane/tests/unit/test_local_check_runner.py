from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.adapters import local_verification
from sidera_control_plane.adapters.local_verification import LocalCheckRunner
from sidera_control_plane.application.result_ports import (
    CheckRunRequest,
    CheckRunStatus,
)
from sidera_control_plane.domain.result_integration import CheckSpecV1


@dataclass
class CheckCase:
    workspace: Path
    scripts: Path
    runner: LocalCheckRunner
    commit: str
    tree: str

    def request(
        self,
        *,
        argv: tuple[str, ...],
        timeout_seconds: float = 2,
        report_paths: tuple[str, ...] = (),
        runner: LocalCheckRunner | None = None,
    ) -> CheckRunRequest:
        check = CheckSpecV1(
            key="unit",
            version=1,
            argv=argv,
            cwd=".",
            environment_ref="test-environment",
            prepare_argvs=(),
            timeout_seconds=max(1, int(timeout_seconds)),
            success_exit_codes=(0,),
            report_paths=report_paths,
            required=True,
        )
        return CheckRunRequest(
            operation_id=uuid4(),
            candidate_id=uuid4(),
            candidate_hash="c" * 64,
            actual_commit=self.commit,
            actual_tree=self.tree,
            check=check,
            workspace_location=self.workspace,
            deadline=datetime.now(UTC) + timedelta(seconds=timeout_seconds),
            log_ref=f".sidera/checks/{uuid4()}",
        )

    def script(self, name: str, source: str) -> Path:
        path = self.scripts / name
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def wait_terminal(
        self, operation_id: UUID, *, runner: LocalCheckRunner | None = None
    ):
        active_runner = runner or self.runner
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observation = active_runner.inspect(operation_id)
            assert observation is not None
            if observation.status is not CheckRunStatus.RUNNING:
                return observation
            time.sleep(0.01)
        raise AssertionError("check did not reach a terminal state")


@pytest.fixture
def check_case(tmp_path: Path) -> CheckCase:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (workspace / "source.txt").write_text("clean\n", encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.invalid")
    _git(workspace, "config", "user.name", "Sidera Test")
    _git(workspace, "add", "source.txt")
    _git(workspace, "commit", "-m", "initial")
    commit = _git(workspace, "rev-parse", "HEAD")
    tree = _git(workspace, "rev-parse", "HEAD^{tree}")
    runner = LocalCheckRunner(
        state_root=tmp_path / "runner-state",
        log_limit_bytes=128,
        report_limit_bytes=32,
    )
    return CheckCase(workspace, scripts, runner, commit, tree)


def test_runner_records_successful_real_exit_and_logs(check_case: CheckCase) -> None:
    script = check_case.script(
        "success.py",
        "import sys\nprint('check passed')\nsys.stdout.flush()\n",
    )
    request = check_case.request(argv=(sys.executable, str(script)))

    started = check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert started.operation_id == request.operation_id
    assert observation.status is CheckRunStatus.PASSED
    assert observation.exit_code == 0
    assert observation.pid is not None
    assert observation.process_start_identity is not None
    assert observation.started_at is not None
    assert observation.finished_at is not None
    assert observation.actual_commit == request.actual_commit
    assert observation.actual_tree == request.actual_tree
    assert observation.source_dirty is False
    assert len(observation.log_refs) == 2
    stdout_path = check_case.workspace / observation.log_refs[0]
    assert "check passed" in stdout_path.read_text(encoding="utf-8")


def test_runner_records_real_nonzero_exit_code(check_case: CheckCase) -> None:
    script = check_case.script(
        "failure.py",
        "raise SystemExit(7)\n",
    )
    request = check_case.request(argv=(sys.executable, str(script)))

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.FAILED
    assert observation.exit_code == 7
    assert observation.failure_code == "check_failed"


def test_runner_reports_process_start_failure_without_fake_exit_fact(
    check_case: CheckCase,
) -> None:
    request = check_case.request(argv=(str(check_case.scripts / "missing"),))

    observation = check_case.runner.start(request)

    assert observation.status is CheckRunStatus.ENVIRONMENT_BLOCKED
    assert observation.exit_code is None
    assert observation.started_at is None
    assert observation.finished_at is None
    assert observation.failure_code == "process_start_failed"


def test_runner_times_out_and_stops_the_process_group(check_case: CheckCase) -> None:
    script = check_case.script(
        "hang.py",
        "import time\ntime.sleep(30)\n",
    )
    request = check_case.request(
        argv=(sys.executable, str(script)),
        timeout_seconds=0.1,
    )

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.TIMED_OUT
    assert observation.failure_code == "deadline_exceeded"
    assert observation.exit_code is not None
    assert observation.finished_at is not None


def test_runner_stops_when_a_log_stream_exceeds_limit(check_case: CheckCase) -> None:
    script = check_case.script(
        "loud.py",
        "import sys\nsys.stdout.write('x' * 4096)\nsys.stdout.flush()\n",
    )
    request = check_case.request(argv=(sys.executable, str(script)))

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.FAILED
    assert observation.failure_code == "log_limit_exceeded"
    stdout_path = check_case.workspace / observation.log_refs[0]
    assert stdout_path.stat().st_size <= 128


def test_runner_records_log_read_error(check_case: CheckCase) -> None:
    def broken_reader(_stream: object, _size: int) -> bytes:
        raise OSError("synthetic stream read failure")

    runner = LocalCheckRunner(
        state_root=check_case.runner.state_root,
        log_limit_bytes=128,
        stream_reader=broken_reader,
    )
    script = check_case.script("read-error.py", "print('ignored')\n")
    request = check_case.request(argv=(sys.executable, str(script)), runner=runner)

    runner.start(request)
    observation = check_case.wait_terminal(request.operation_id, runner=runner)

    assert observation.status is CheckRunStatus.ENVIRONMENT_BLOCKED
    assert observation.failure_code == "log_read_error"
    assert observation.exit_code is not None


def test_runner_finalizes_log_read_error_after_identity_race(
    check_case: CheckCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_reader(_stream: object, _size: int) -> bytes:
        raise OSError("synthetic stream read failure")

    runner = LocalCheckRunner(
        state_root=check_case.runner.state_root,
        log_limit_bytes=128,
        stream_reader=broken_reader,
    )
    script = check_case.script(
        "read-error-race.py",
        "import time\nprint('ignored')\ntime.sleep(0.05)\n",
    )
    request = check_case.request(argv=(sys.executable, str(script)), runner=runner)
    monkeypatch.setattr(local_verification, "_identity_matches", lambda _identity: False)

    runner.start(request)
    observation = check_case.wait_terminal(request.operation_id, runner=runner)

    assert observation.status is CheckRunStatus.ENVIRONMENT_BLOCKED
    assert observation.failure_code == "log_read_error"
    assert observation.exit_code is not None


def test_runner_reports_missing_configured_report(check_case: CheckCase) -> None:
    script = check_case.script("no-report.py", "print('no report')\n")
    request = check_case.request(
        argv=(sys.executable, str(script)),
        report_paths=("reports/result.json",),
    )

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.FAILED
    assert observation.failure_code == "report_missing"
    assert observation.report_availability[0].path == "reports/result.json"
    assert observation.report_availability[0].status == "missing"
    assert observation.report_availability[0].sha256 is None


def test_runner_hashes_present_report(check_case: CheckCase) -> None:
    script = check_case.script(
        "report.py",
        "from pathlib import Path\nPath('reports').mkdir()\nPath('reports/result.json').write_text('x' * 16)\n",
    )
    request = check_case.request(
        argv=(sys.executable, str(script)),
        report_paths=("reports/result.json",),
    )

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.PASSED
    report = observation.report_availability[0]
    assert report.status == "present"
    assert report.size_bytes == 16
    assert report.sha256 == hashlib.sha256(b"x" * 16).hexdigest()


def test_runner_rejects_oversized_report(check_case: CheckCase) -> None:
    script = check_case.script(
        "report.py",
        "from pathlib import Path\nPath('reports').mkdir()\nPath('reports/result.json').write_text('x' * 64)\n",
    )
    request = check_case.request(
        argv=(sys.executable, str(script)),
        report_paths=("reports/result.json",),
    )

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.FAILED
    assert observation.failure_code == "report_too_large"
    report = observation.report_availability[0]
    assert report.status == "too_large"
    assert report.size_bytes == 64
    assert report.sha256 is None


def test_runner_rejects_remote_report_reference(check_case: CheckCase) -> None:
    request = check_case.request(
        argv=(sys.executable, "-c", "print('unused')"),
        report_paths=("https://example.invalid/report.json",),
    )

    observation = check_case.runner.start(request)

    assert observation.status is CheckRunStatus.ENVIRONMENT_BLOCKED
    assert observation.failure_code == "check_path_invalid"


def test_runner_marks_source_modified_during_check(check_case: CheckCase) -> None:
    script = check_case.script(
        "modify.py",
        "from pathlib import Path\nPath('source.txt').write_text('changed\\n')\n",
    )
    request = check_case.request(argv=(sys.executable, str(script)))

    check_case.runner.start(request)
    observation = check_case.wait_terminal(request.operation_id)

    assert observation.status is CheckRunStatus.ENVIRONMENT_BLOCKED
    assert observation.source_dirty is True
    assert observation.failure_code == "source_modified_during_check"


def test_restarted_runner_returns_unknown_without_restarting_process(
    check_case: CheckCase,
) -> None:
    script = check_case.script("restart.py", "import time\ntime.sleep(30)\n")
    request = check_case.request(argv=(sys.executable, str(script)))
    check_case.runner.start(request)

    state_path = check_case.runner.state_root / f"{request.operation_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["process_start_identity"]["pid"] = 999999
    state["pid"] = 999999
    state_path.write_text(json.dumps(state), encoding="utf-8")

    restarted = LocalCheckRunner(state_root=check_case.runner.state_root)
    observation = restarted.inspect(request.operation_id)

    assert observation is not None
    assert observation.status is CheckRunStatus.UNKNOWN
    assert observation.exit_code is None
    assert observation.finished_at is None
    assert observation.failure_code == "process_state_unknown"

    check_case.runner.stop(request.operation_id)


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
