from __future__ import annotations

import os
import selectors
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidera_agents.contracts import NativeProcessState
from sidera_agents.harnesses import acp_transport
from sidera_agents.harnesses.acp_transport import ACPProcessTransport, ACPTransportError


def _spawn(tmp_path: Path, code: str) -> ACPProcessTransport:
    return ACPProcessTransport.spawn_argv(
        (sys.executable, "-u", "-c", code),
        cwd=tmp_path,
        environment={"PATH": os.defpath, "LANG": "C"},
    )


def test_read_available_zero_timeout_drains_ready_notification(tmp_path):
    code = (
        "import sys\n"
        "sys.stdin.buffer.readline()\n"
        'sys.stdout.buffer.write(b\'{"jsonrpc":"2.0","method":"fixture/ready","params":{}}\\n\')\n'
        "sys.stdout.buffer.flush()\n"
        "sys.stdin.buffer.readline()\n"
    )
    transport = _spawn(tmp_path, code)

    try:
        transport.notify("fixture/emit", {})
        with selectors.DefaultSelector() as selector:
            selector.register(transport._stdout, selectors.EVENT_READ)
            assert selector.select(1)

        transport.read_available(timeout=0)

        assert transport.drain_notifications() == (
            {
                "jsonrpc": "2.0",
                "method": "fixture/ready",
                "params": {},
            },
        )
        started = time.monotonic()
        transport.read_available(timeout=0)
        assert time.monotonic() - started < 0.1
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_request_timeout_includes_lock_wait(tmp_path):
    peer = (
        "import json,sys\n"
        "for line in sys.stdin.buffer:\n"
        " request=json.loads(line)\n"
        " if 'id' in request:\n"
        "  response={'jsonrpc':'2.0','id':request['id'],'result':{}}\n"
        "  sys.stdout.buffer.write(json.dumps(response).encode()+b'\\n')\n"
        "  sys.stdout.buffer.flush()\n"
    )
    transport = _spawn(tmp_path, peer)
    assert transport._request_lock.acquire()
    started = time.monotonic()
    result = {}
    completed = threading.Event()

    def request():
        try:
            result["response"] = transport.request("fixture/lock", {}, timeout=0.05)
        except ACPTransportError as error:
            result["reason"] = error.reason
        finally:
            completed.set()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    try:
        completed.wait(0.16)
    finally:
        transport._request_lock.release()
    worker.join(1)

    try:
        assert not worker.is_alive()
        assert result.get("reason") == "acp_timeout"
        assert time.monotonic() - started <= 0.15
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_request_deadline_rejects_late_response_after_notifications(
    tmp_path, monkeypatch
):
    release_response = tmp_path / "release-response"
    response_ready = tmp_path / "response-ready"
    peer = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "first=json.loads(sys.stdin.buffer.readline())\n"
        "notification={'jsonrpc':'2.0','method':'fixture/tick','params':{}}\n"
        "sys.stdout.buffer.write((json.dumps(notification)+'\\n').encode())\n"
        "sys.stdout.buffer.flush()\n"
        f"while not Path({str(release_response)!r}).exists(): time.sleep(0.005)\n"
        "late={'jsonrpc':'2.0','id':first['id'],'result':{'late':True}}\n"
        "sys.stdout.buffer.write((json.dumps(late)+'\\n').encode())\n"
        "sys.stdout.buffer.flush()\n"
        f"Path({str(response_ready)!r}).write_text('ready')\n"
        "second=json.loads(sys.stdin.buffer.readline())\n"
        "result={'jsonrpc':'2.0','id':second['id'],'result':{'method':second['method']}}\n"
        "sys.stdout.buffer.write((json.dumps(result)+'\\n').encode())\n"
        "sys.stdout.buffer.flush()\n"
    )
    transport = _spawn(tmp_path, peer)
    notification_seen = threading.Event()
    resume_request = threading.Event()
    completed = threading.Event()
    result = {}
    original_handler = transport._handle_incoming_method

    def pause_after_notification(message, *, deadline=None):
        original_handler(message, deadline=deadline)
        if message.get("method") == "fixture/tick":
            notification_seen.set()
            assert resume_request.wait(timeout=2)

    monkeypatch.setattr(transport, "_handle_incoming_method", pause_after_notification)
    timeout = 0.12
    request_started = time.monotonic()

    def request_first():
        try:
            result["first"] = transport.request("fixture/first", {}, timeout=timeout)
        except ACPTransportError as error:
            result["error"] = error.reason
        finally:
            completed.set()

    worker = threading.Thread(target=request_first, daemon=True)
    worker.start()
    try:
        assert notification_seen.wait(timeout=1)
        deadline = request_started + timeout + 0.02
        while time.monotonic() < deadline:
            time.sleep(0.002)

        release_response.touch()
        response_deadline = time.monotonic() + 1
        while not response_ready.exists() and time.monotonic() < response_deadline:
            time.sleep(0.002)
        assert response_ready.exists()
        with selectors.DefaultSelector() as selector:
            selector.register(transport._stdout, selectors.EVENT_READ)
            assert selector.select(1)
        buffered_response = os.read(transport._stdout.fileno(), 4096)
        assert buffered_response.endswith(b"\n")
        transport._read_buffer.extend(buffered_response)

        resume_request.set()
        assert completed.wait(timeout=1)
        assert result.get("error") == "acp_timeout"

        next_response = transport.request("fixture/second", {}, timeout=1)
        assert next_response == {"method": "fixture/second"}
        assert transport.drain_late_responses() == (
            (
                "fixture/first",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"late": True},
                },
            ),
        )
    finally:
        release_response.touch()
        resume_request.set()
        worker.join(timeout=1)
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_request_rejects_complete_buffered_response_after_deadline(
    tmp_path, monkeypatch
):
    transport = _spawn(tmp_path, "import time\ntime.sleep(10)")
    transport._read_buffer.extend(b'{"jsonrpc":"2.0","id":1,"result":{"late":true}}\n')
    clock = {"now": 100.0}

    def monotonic():
        return clock["now"]

    original_write = transport._write

    def advance_past_deadline(message, *, deadline=None):
        original_write(message, deadline=deadline)
        clock["now"] = deadline + 0.001

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(acp_transport, "time", SimpleNamespace(monotonic=monotonic))
            scoped.setattr(transport, "_write", advance_past_deadline)
            with pytest.raises(ACPTransportError) as error:
                transport.request("fixture/buffered", {}, timeout=0.1)

        assert error.value.reason == "acp_timeout"
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_request_timeout_bounds_partial_pipe_write_and_poisons_transport(tmp_path):
    transport = _spawn(tmp_path, "import time\ntime.sleep(10)")
    result = {}
    completed = threading.Event()
    started = time.monotonic()

    def request():
        try:
            result["response"] = transport.request(
                "fixture/large", {"data": "x" * 1_500_000}, timeout=0.05
            )
        except ACPTransportError as error:
            result["reason"] = error.reason
        finally:
            completed.set()

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    finished_before_cleanup = completed.wait(0.16)
    elapsed_before_cleanup = time.monotonic() - started
    if not finished_before_cleanup:
        transport.terminate(grace_seconds=0.2)
    worker.join(1)

    try:
        assert finished_before_cleanup
        assert elapsed_before_cleanup <= 0.15
        assert result.get("reason") == "acp_timeout"
        with pytest.raises(ACPTransportError) as error:
            transport.request("fixture/after-timeout", {}, timeout=0.05)
        assert error.value.reason == "acp_transport_unusable"
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


@pytest.mark.parametrize("failure", ["permission", "start_identity"])
def test_identity_probe_failure_is_unknown(tmp_path, monkeypatch, failure):
    transport = _spawn(tmp_path, "import time\ntime.sleep(10)")

    try:
        with monkeypatch.context() as scoped:
            if failure == "permission":

                def denied(_pid):
                    raise PermissionError

                scoped.setattr(acp_transport.os, "getpgid", denied)
            else:

                def unavailable(_pid):
                    raise acp_transport.ACPTransportError("acp_identity_unavailable")

                scoped.setattr(acp_transport, "_process_start_identity", unavailable)

            state = transport.identity_state(transport.identity)

        assert state is NativeProcessState.UNKNOWN
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_identity_probe_distinguishes_exit_and_replacement(tmp_path, monkeypatch):
    transport = _spawn(tmp_path, "import time\ntime.sleep(10)")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                acp_transport, "_process_start_identity", lambda _pid: "replacement"
            )
            assert (
                transport.identity_state(transport.identity) is NativeProcessState.GONE
            )

        transport.process.terminate()
        transport.process.wait(timeout=1)
        assert transport.identity_state(transport.identity) is NativeProcessState.GONE
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_terminate_does_not_signal_after_owner_identity_mismatch(tmp_path, monkeypatch):
    transport = _spawn(tmp_path, "import time\ntime.sleep(10)")
    signals = []

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                acp_transport, "_process_start_identity", lambda _pid: "replacement"
            )
            scoped.setattr(
                acp_transport,
                "_signal_process_group",
                lambda process, group, sig: signals.append((process.pid, group, sig)),
            )
            assert not transport.terminate(grace_seconds=0.05)

        assert signals == []
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_terminate_retries_transient_unknown_group_probe_after_signal(
    tmp_path, monkeypatch
):
    transport = _spawn(tmp_path, "import time\ntime.sleep(10)")
    probe_count = 0
    probe_states = (True, None, False)

    def transient_unknown(_group_id):
        nonlocal probe_count
        state = probe_states[probe_count]
        probe_count += 1
        if state is False:
            time.sleep(0.05)
        return state

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(acp_transport, "_process_group_exists", transient_unknown)
            closed = transport.terminate(grace_seconds=0.5)
        assert closed
        assert probe_count == 3
        assert transport.process.returncode is not None
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


@pytest.mark.skipif(os.name != "posix", reason="requires process groups and fork")
def test_terminate_waits_for_owned_process_group_after_leader_exit(tmp_path):
    ready = tmp_path / "descendant-ready"
    exited = tmp_path / "descendant-exited"
    code = (
        "import os, signal, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(0.25)\n"
        f"    open({str(exited)!r}, 'w').write('done')\n"
        "    os._exit(0)\n"
        f"open({str(ready)!r}, 'w').write(str(child))\n"
        "while True: time.sleep(1)\n"
    )
    transport = _spawn(tmp_path, code)

    try:
        deadline = time.monotonic() + 1
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ready.exists()

        started = time.monotonic()
        closed = transport.terminate(grace_seconds=0.7)
        elapsed = time.monotonic() - started

        assert closed
        assert elapsed >= 0.15
        assert exited.read_text() == "done"
        with pytest.raises(ProcessLookupError):
            os.killpg(transport.process.pid, 0)
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


@pytest.mark.skipif(os.name != "posix", reason="requires process groups and fork")
def test_identity_state_waits_for_owned_group_absence_after_leader_exit(tmp_path):
    ready = tmp_path / "descendant-ready"
    release = tmp_path / "descendant-release"
    exited = tmp_path / "descendant-exited"
    code = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    Path({str(ready)!r}).write_text('ready')\n"
        f"    while not Path({str(release)!r}).exists(): time.sleep(0.005)\n"
        f"    Path({str(exited)!r}).write_text('done')\n"
        "    os._exit(0)\n"
        "while True: time.sleep(1)\n"
    )
    transport = _spawn(tmp_path, code)
    group_id = transport._process_group_id

    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ready.exists()
        assert transport.identity_state(transport.identity) is NativeProcessState.ALIVE

        transport.process.terminate()
        transport.process.wait(timeout=1)

        assert acp_transport._process_group_exists(group_id) is True
        assert (
            transport.identity_state(transport.identity) is NativeProcessState.UNKNOWN
        )

        release.touch()
        deadline = time.monotonic() + 2
        while not exited.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert exited.exists()
        while (
            acp_transport._process_group_exists(group_id) is not False
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert acp_transport._process_group_exists(group_id) is False
        assert transport.identity_state(transport.identity) is NativeProcessState.GONE
    finally:
        release.touch()
        if transport.process.poll() is None:
            transport.process.terminate()
            transport.process.wait(timeout=1)
        cleanup_deadline = time.monotonic() + 2
        while (
            acp_transport._process_group_exists(group_id) is not False
            and time.monotonic() < cleanup_deadline
        ):
            time.sleep(0.005)
        transport.close()
