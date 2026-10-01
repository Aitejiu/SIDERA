import os
import sys
import time
from uuid import uuid4

import pytest

import sidera_agents.runtimes as runtime_module
from sidera_agents.contracts import ProfileDraft, RuntimeCandidate
from sidera_agents.errors import AgentError
from sidera_agents.harnesses import opencode_descriptor


def _executable_script(tmp_path, body):
    executable = tmp_path / "runtime probe" / "opencode"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _pipe_inheriting_descendant_probe(tmp_path, *, overflow=False):
    started = tmp_path / "descendant-started-marker"
    release = tmp_path / "descendant-release-marker"
    late = tmp_path / "descendant-late-marker"
    body = (
        "import os, time\n"
        "ready_read, ready_write = os.pipe()\n"
        "child_pid = os.fork()\n"
        "if child_pid == 0:\n"
        "    os.close(ready_read)\n"
        f"    with open({str(started)!r}, 'w', encoding='utf-8') as output:\n"
        "        output.write(str(os.getpid()))\n"
        "    os.write(ready_write, b'ready')\n"
        "    os.close(ready_write)\n"
        f"    while not os.path.exists({str(release)!r}):\n"
        "        time.sleep(0.01)\n"
        f"    with open({str(late)!r}, 'w', encoding='utf-8') as output:\n"
        "        output.write('late')\n"
        "    raise SystemExit(0)\n"
        "os.close(ready_write)\n"
        "assert os.read(ready_read, 5) == b'ready'\n"
        "os.close(ready_read)\n"
    )
    if overflow:
        body += f"os.write(2, b'x' * {opencode_descriptor._MAX_OUTPUT_BYTES + 1})\n"
    body += "os._exit(0)\n"
    return _executable_script(tmp_path, body), started, release, late


def _wait_for_marker(marker, timeout):
    deadline = time.monotonic() + timeout
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return marker.exists()


def _wait_for_process_exit(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def _assert_probe_abort_kills_descendant(
    executable, started, release, late, expected_code
):
    try:
        with pytest.raises(AgentError) as error:
            opencode_descriptor.read_version(str(executable))

        assert error.value.code == expected_code
        assert started.exists(), "synthetic descendant did not start"
        assert not late.exists(), "descendant wrote before the release marker"
        release.touch()
        assert not _wait_for_marker(late, 1.0), (
            "aborted probe descendant wrote after read_version returned"
        )
    finally:
        release.touch()
        if started.exists():
            child_pid = int(started.read_text(encoding="utf-8"))
            assert _wait_for_process_exit(child_pid, 2.0)


def test_version_probe_uses_fixed_argv_and_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDERA_PROBE_SENTINEL", "must-not-reach-child")
    executable = _executable_script(
        tmp_path,
        "import os, sys\n"
        "assert sys.argv[1:] == ['--version']\n"
        "assert 'SIDERA_PROBE_SENTINEL' not in os.environ\n"
        "print('OpenCode v1.4.2')",
    )

    assert opencode_descriptor.read_version(str(executable)) == "1.4.2"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_version_probe_bounds_output_and_hides_raw_output(tmp_path, stream):
    marker = "synthetic-probe-secret"
    statement = f"sys.{stream}.write({marker!r} * 4096)"
    executable = _executable_script(
        tmp_path,
        "import sys\n" + statement,
    )

    with pytest.raises(AgentError) as error:
        opencode_descriptor.read_version(str(executable))

    assert error.value.code == "runtime_probe_output_too_large"
    assert marker not in str(error.value)


def test_version_probe_timeout_is_bounded_and_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_descriptor, "_PROBE_TIMEOUT_SECONDS", 0.1)
    executable = _executable_script(
        tmp_path,
        "import time\ntime.sleep(2)",
    )

    with pytest.raises(AgentError) as error:
        opencode_descriptor.read_version(str(executable))

    assert error.value.code == "runtime_probe_timeout"
    assert "synthetic" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX fork semantics")
def test_version_probe_timeout_kills_inherited_pipe_descendant(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_descriptor, "_PROBE_TIMEOUT_SECONDS", 1.5)
    executable, started, release, late = _pipe_inheriting_descendant_probe(tmp_path)

    _assert_probe_abort_kills_descendant(
        executable,
        started,
        release,
        late,
        "runtime_probe_timeout",
    )


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX fork semantics")
def test_version_probe_output_overflow_kills_inherited_pipe_descendant(
    tmp_path, monkeypatch
):
    executable, started, release, late = _pipe_inheriting_descendant_probe(
        tmp_path, overflow=True
    )

    _assert_probe_abort_kills_descendant(
        executable,
        started,
        release,
        late,
        "runtime_probe_output_too_large",
    )


def test_discovery_register_and_refresh_use_the_selected_target_without_prompting(
    runtime_lab, monkeypatch
):
    selected = runtime_lab.executable.resolve(strict=True)
    which_calls = []
    probe_io = type(
        "ProbeIO",
        (),
        {"version_calls": [], "protocol_calls": [], "prompt_calls": []},
    )()

    def which(command):
        which_calls.append(command)
        return str(selected) if command == "opencode" else None

    def read_version(executable):
        probe_io.version_calls.append(executable)
        return "1.4.2"

    monkeypatch.setattr(runtime_module.shutil, "which", which)
    monkeypatch.setattr(opencode_descriptor, "read_version", read_version)

    candidates = runtime_lab.runtimes.discover(
        runtime_lab.workspace_id, grant_id=runtime_lab.grant_id
    )
    assert candidates == (
        RuntimeCandidate(harness_id="opencode", executable=str(selected)),
    )

    registered = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        candidates[0].executable,
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )
    refreshed = runtime_lab.runtimes.refresh(
        registered.ref, grant_id=runtime_lab.grant_id
    )

    assert registered.ref == refreshed.ref
    assert refreshed.executable == str(selected)
    assert refreshed.actual_version == "1.4.2"
    assert refreshed.protocol_state == "unconfirmed"
    assert refreshed.auth_state == "unconfirmed"
    assert refreshed.limits == {"max_active": 2, "max_sessions": 4}
    assert which_calls == ["opencode"]
    assert probe_io.version_calls == [str(selected), str(selected)]
    assert probe_io.protocol_calls == []
    assert probe_io.prompt_calls == []


def test_workspace_grant_cannot_edit_shared_installation_limits(
    runtime_lab, monkeypatch
):
    monkeypatch.setattr(opencode_descriptor, "read_version", lambda _: "1.4.2")
    registered = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(runtime_lab.executable),
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )

    with pytest.raises(AgentError) as error:
        runtime_lab.runtimes.set_runtime_limits(
            registered.ref,
            max_active=5,
            max_sessions=7,
            expected_version=1,
            grant_id=runtime_lab.grant_id,
        )

    assert error.value.code == "permission_denied"
    assert runtime_lab.runtimes.get_runtime(
        registered.ref, grant_id=runtime_lab.grant_id
    ).limits == {"max_active": 2, "max_sessions": 4}


def test_alias_registration_and_many_profiles_preserve_installation_configuration(
    runtime_lab, monkeypatch, tmp_path
):
    probe_calls = []
    monkeypatch.setattr(
        opencode_descriptor,
        "read_version",
        lambda executable: probe_calls.append(executable) or "1.4.2",
    )
    registered = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(runtime_lab.executable),
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )
    configured = runtime_lab.runtimes.set_runtime_limits(
        registered.ref,
        max_active=5,
        max_sessions=7,
        expected_version=1,
        grant_id=runtime_lab.admin_grant_id,
    )
    alias = tmp_path / "opencode-alias"
    alias.symlink_to(runtime_lab.executable)

    repeated = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(alias),
        max_active=20,
        max_sessions=30,
        grant_id=runtime_lab.grant_id,
    )
    for index in range(3):
        runtime_lab.catalog.create_profile(
            runtime_lab.workspace_id,
            ProfileDraft(
                name=f"profile-{index}",
                description="Test profile",
                runtime_ref=registered.ref,
                instructions="Work safely.",
                resource_refs=(),
                model_id=None,
                thinking=None,
                max_active=1,
                environment={},
                secret_refs=(),
                native_settings={},
            ),
            grant_id=runtime_lab.grant_id,
        )

    assert repeated.ref == registered.ref
    assert repeated.version == configured.version == 2
    assert repeated.limits == {"max_active": 5, "max_sessions": 7}
    assert probe_calls == [str(runtime_lab.executable.resolve())]


def test_distinct_workspace_references_share_installation_limits_but_not_reads(
    runtime_lab, monkeypatch
):
    workspace_b = uuid4()
    grant_b = uuid4()
    runtime_lab.authorizer.grant_workspace(workspace_b, grant_b)
    monkeypatch.setattr(opencode_descriptor, "read_version", lambda _: "1.4.2")

    first = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(runtime_lab.executable),
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )
    second = runtime_lab.runtimes.register(
        workspace_b,
        "opencode",
        str(runtime_lab.executable),
        max_active=20,
        max_sessions=40,
        grant_id=grant_b,
    )
    changed = runtime_lab.runtimes.set_runtime_limits(
        first.ref,
        max_active=3,
        max_sessions=6,
        expected_version=1,
        grant_id=runtime_lab.admin_grant_id,
    )

    visible_from_b = runtime_lab.runtimes.get_runtime(second.ref, grant_id=grant_b)
    assert first.ref != second.ref
    assert first.ref.workspace_id != second.ref.workspace_id
    assert changed.limits == {"max_active": 3, "max_sessions": 6}
    assert visible_from_b.version == changed.version
    assert visible_from_b.limits == changed.limits
    with pytest.raises(AgentError) as error:
        runtime_lab.runtimes.get_runtime(second.ref, grant_id=runtime_lab.grant_id)
    assert error.value.code == "permission_denied"


def test_runtime_configuration_uses_cas_and_refresh_only_changes_observations(
    runtime_lab, monkeypatch
):
    versions = iter(("1.4.2", "2.0.0"))
    monkeypatch.setattr(opencode_descriptor, "read_version", lambda _: next(versions))
    registered = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(runtime_lab.executable),
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )
    configured = runtime_lab.runtimes.set_runtime_limits(
        registered.ref,
        max_active=3,
        max_sessions=5,
        expected_version=1,
        grant_id=runtime_lab.admin_grant_id,
    )
    refreshed = runtime_lab.runtimes.refresh(
        configured.ref, grant_id=runtime_lab.grant_id
    )

    assert refreshed.actual_version == "2.0.0"
    assert refreshed.executable == registered.executable
    assert refreshed.version == configured.version == 2
    assert refreshed.limits == configured.limits
    with pytest.raises(AgentError) as error:
        runtime_lab.runtimes.set_runtime_enabled(
            refreshed.ref,
            False,
            expected_version=1,
            grant_id=runtime_lab.admin_grant_id,
        )
    assert error.value.code == "revision_conflict"


def test_refresh_marks_moved_executable_unavailable_without_substitution(
    runtime_lab, monkeypatch
):
    monkeypatch.setattr(opencode_descriptor, "read_version", lambda _: "1.4.2")
    registered = runtime_lab.runtimes.register(
        runtime_lab.workspace_id,
        "opencode",
        str(runtime_lab.executable),
        max_active=2,
        max_sessions=4,
        grant_id=runtime_lab.grant_id,
    )
    replacement = runtime_lab.executable.with_name("other-opencode")
    runtime_lab.executable.rename(replacement)

    refreshed = runtime_lab.runtimes.refresh(
        registered.ref, grant_id=runtime_lab.grant_id
    )

    assert refreshed.executable == registered.executable
    assert refreshed.installed is False
    assert refreshed.actual_version == registered.actual_version
    assert refreshed.protocol_state == "unavailable"
    assert refreshed.version == registered.version
