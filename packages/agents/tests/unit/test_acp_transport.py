from __future__ import annotations

import json
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


def _permission_request_source(request_id: str) -> str:
    return (
        "server_request = {\n"
        "    'jsonrpc': '2.0',\n"
        f"    'id': {request_id!r},\n"
        "    'method': 'session/request_permission',\n"
        "    'params': {'options': [{'kind': 'reject_once', 'optionId': 'reject-once'}]},\n"
        "}\n"
        "sys.stdout.buffer.write((json.dumps(server_request) + '\\n').encode())\n"
        "sys.stdout.buffer.flush()\n"
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


def test_request_timeout_preserves_server_request_for_zero_timeout_drain(
    tmp_path, monkeypatch
):
    response_file = tmp_path / "permission-response.json"
    stop_peer = tmp_path / "stop-peer"
    peer = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "json.loads(sys.stdin.buffer.readline())\n"
        + _permission_request_source("permission-expired")
        + "response=json.loads(sys.stdin.buffer.readline())\n"
        + f"Path({str(response_file)!r}).write_text(json.dumps(response))\n"
        + f"while not Path({str(stop_peer)!r}).exists(): time.sleep(0.005)\n"
    )
    transport = _spawn(tmp_path, peer)
    consumed = threading.Event()
    resume = threading.Event()
    finished = threading.Event()
    result = {}
    read_message = transport._read_message

    def pause_after_consuming_request(timeout, *, deadline=None):
        message = read_message(timeout, deadline=deadline)
        if message.get("id") == "permission-expired":
            consumed.set()
            assert resume.wait(timeout=2)
        return message

    monkeypatch.setattr(transport, "_read_message", pause_after_consuming_request)

    def run_request():
        try:
            transport.request("fixture/expire", {}, timeout=0.12)
        except ACPTransportError as error:
            result["error"] = error.reason
        finally:
            finished.set()

    worker = threading.Thread(target=run_request, daemon=True)
    worker.start()
    try:
        assert consumed.wait(timeout=1)
        assert not resume.wait(timeout=0.16)
        resume.set()
        assert finished.wait(timeout=1)
        worker.join(timeout=1)
        assert result == {"error": "acp_timeout"}

        transport.read_available(timeout=0)
        response_deadline = time.monotonic() + 1
        while not response_file.exists() and time.monotonic() < response_deadline:
            time.sleep(0.005)
        assert response_file.exists()
        assert json.loads(response_file.read_text()) == {
            "jsonrpc": "2.0",
            "id": "permission-expired",
            "result": {"outcome": {"outcome": "selected", "optionId": "reject-once"}},
        }
        assert transport.drain_notifications() == ()
    finally:
        resume.set()
        stop_peer.touch()
        worker.join(timeout=1)
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_expired_request_normalizes_deferred_reply_failure(tmp_path, monkeypatch):
    peer = (
        "import json,sys,time\n"
        "json.loads(sys.stdin.buffer.readline())\n"
        + _permission_request_source("permission-expired-write-error")
        + "time.sleep(10)\n"
    )
    transport = _spawn(tmp_path, peer)
    consumed = threading.Event()
    resume = threading.Event()
    finished = threading.Event()
    result = {}
    original_read = transport._read_message

    def pause_after_consuming_request(timeout, *, deadline=None):
        message = original_read(timeout, deadline=deadline)
        if message.get("id") == "permission-expired-write-error":
            consumed.set()
            assert resume.wait(timeout=2)
        return message

    original_write = transport._write

    def fail_deferred_reply(message, **kwargs):
        if message.get("id") == "permission-expired-write-error":
            raise ACPTransportError("acp_process_closed")
        return original_write(message, **kwargs)

    monkeypatch.setattr(transport, "_read_message", pause_after_consuming_request)
    monkeypatch.setattr(transport, "_write", fail_deferred_reply)

    def run_request():
        try:
            transport.request("fixture/expire", {}, timeout=0.12)
        except ACPTransportError as error:
            result["error"] = error.reason
        finally:
            finished.set()

    worker = threading.Thread(target=run_request, daemon=True)
    worker.start()
    try:
        assert consumed.wait(timeout=1)
        assert not resume.wait(timeout=0.16)
        resume.set()
        assert finished.wait(timeout=1)
        worker.join(timeout=1)

        assert result == {"error": "acp_timeout"}
        assert list(transport._pending_server_requests) == [
            {
                "jsonrpc": "2.0",
                "id": "permission-expired-write-error",
                "method": "session/request_permission",
                "params": {
                    "options": [{"kind": "reject_once", "optionId": "reject-once"}]
                },
            }
        ]
        assert transport.drain_notifications() == ()
        assert transport.drain_late_responses() == ()
    finally:
        resume.set()
        worker.join(timeout=1)
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_read_available_zero_timeout_retries_queued_server_request(tmp_path):
    response_file = tmp_path / "permission-response.json"
    check_extra = tmp_path / "check-extra"
    checked = tmp_path / "checked"
    extra_response = tmp_path / "extra-response.json"
    peer = (
        "import json,select,sys,time\n"
        "from pathlib import Path\n"
        + _permission_request_source("permission-retry")
        + "response=json.loads(sys.stdin.buffer.readline())\n"
        + f"Path({str(response_file)!r}).write_text(json.dumps(response))\n"
        + f"while not Path({str(check_extra)!r}).exists(): time.sleep(0.005)\n"
        + "if select.select([sys.stdin.buffer], [], [], 0.1)[0]:\n"
        + "    extra=sys.stdin.buffer.readline()\n"
        + f"    Path({str(extra_response)!r}).write_bytes(extra)\n"
        + f"Path({str(checked)!r}).touch()\n"
        + "time.sleep(10)\n"
    )
    transport = _spawn(tmp_path, peer)
    worker_done = threading.Event()
    worker_errors = []
    worker = threading.Thread(
        target=lambda: _capture_read_available(transport, worker_done, worker_errors),
        daemon=True,
    )
    lock_held = False

    try:
        assert transport._write_lock.acquire(timeout=1)
        lock_held = True
        with selectors.DefaultSelector() as selector:
            selector.register(transport._stdout, selectors.EVENT_READ)
            assert selector.select(1)

        worker.start()
        returned_while_locked = worker_done.wait(timeout=0.1)
        transport._write_lock.release()
        lock_held = False
        worker.join(timeout=1)

        assert returned_while_locked
        assert not worker.is_alive()
        assert worker_errors == []
        assert len(getattr(transport, "_pending_server_requests", ())) == 1
        assert not response_file.exists()

        transport.read_available(timeout=0)
        response_deadline = time.monotonic() + 1
        while not response_file.exists() and time.monotonic() < response_deadline:
            time.sleep(0.005)
        assert response_file.exists()
        assert json.loads(response_file.read_text()) == {
            "jsonrpc": "2.0",
            "id": "permission-retry",
            "result": {"outcome": {"outcome": "selected", "optionId": "reject-once"}},
        }
        assert len(getattr(transport, "_pending_server_requests", ())) == 0
        assert transport.drain_notifications() == ()

        transport.read_available(timeout=0)
        check_extra.touch()
        assert _wait_for_path(checked, 1)
        assert not extra_response.exists()
    finally:
        if lock_held:
            transport._write_lock.release()
        check_extra.touch()
        if worker.ident is not None:
            worker.join(timeout=1)
        transport.terminate(grace_seconds=0.2)
        transport.close()


def _capture_read_available(transport, done, errors):
    try:
        transport.read_available(timeout=0)
    except ACPTransportError as error:
        errors.append(error.reason)
    finally:
        done.set()


def test_request_drains_deferred_permission_before_next_request(tmp_path, monkeypatch):
    first_client_seen = tmp_path / "first-client-seen"
    emit_server_request = tmp_path / "emit-server-request"
    peer_events = tmp_path / "peer-events.json"
    peer = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "first=json.loads(sys.stdin.buffer.readline())\n"
        f"Path({str(first_client_seen)!r}).touch()\n"
        f"while not Path({str(emit_server_request)!r}).exists(): time.sleep(0.005)\n"
        + _permission_request_source("permission-deferred")
        + "server_response=json.loads(sys.stdin.buffer.readline())\n"
        + "events=[{'kind':'server_response','message':server_response}]\n"
        + "next_request=json.loads(sys.stdin.buffer.readline())\n"
        + "events.append({'kind':'client_request','method':next_request['method']})\n"
        + f"Path({str(peer_events)!r}).write_text(json.dumps(events))\n"
        + "response={'jsonrpc':'2.0','id':next_request['id'],'result':{'ready':True}}\n"
        + "sys.stdout.buffer.write((json.dumps(response)+'\\n').encode())\n"
        + "sys.stdout.buffer.flush()\n"
    )
    transport = _spawn(tmp_path, peer)
    request_consumed = threading.Event()
    first_done = threading.Event()
    first_result = {}
    write_attempt_deadlines = []
    original_handler = transport._handle_incoming_method
    original_write = transport._write

    def observe_server_request(message, **kwargs):
        if message.get("id") == "permission-deferred":
            request_consumed.set()
        return original_handler(message, **kwargs)

    def timeout_deferred_reply(message, **kwargs):
        if message.get("id") == "permission-deferred":
            write_attempt_deadlines.append(kwargs.get("deadline"))
            if len(write_attempt_deadlines) == 1:
                raise ACPTransportError("acp_timeout")
            if len(write_attempt_deadlines) == 2:
                deadline = kwargs.get("deadline")
                if deadline is not None:
                    threading.Event().wait(max(0.0, deadline - time.monotonic()))
                raise ACPTransportError("acp_timeout")
        return original_write(message, **kwargs)

    monkeypatch.setattr(transport, "_handle_incoming_method", observe_server_request)
    monkeypatch.setattr(transport, "_write", timeout_deferred_reply)

    def first_request():
        try:
            transport.request("fixture/initial", {}, timeout=0.5)
        except ACPTransportError as error:
            first_result["error"] = error.reason
        finally:
            first_done.set()

    first_worker = threading.Thread(target=first_request, daemon=True)
    first_worker.start()
    blocked_done = threading.Event()
    blocked_started = threading.Event()
    blocked_result = {}
    request_lock_held = False
    blocked_worker = None

    try:
        assert first_client_seen.exists() or _wait_for_path(first_client_seen, 1)
        emit_server_request.touch()
        assert request_consumed.wait(timeout=1)
        assert first_done.wait(timeout=1)
        first_worker.join(timeout=1)
        assert first_result == {"error": "acp_timeout"}

        assert transport._request_lock.acquire(timeout=1)
        request_lock_held = True

        def blocked_request():
            blocked_started.set()
            try:
                transport.request("fixture/must-not-send", {}, timeout=0.18)
            except ACPTransportError as error:
                blocked_result["error"] = error.reason
            finally:
                blocked_done.set()

        blocked_start_time = time.monotonic()
        blocked_worker = threading.Thread(target=blocked_request, daemon=True)
        blocked_worker.start()
        assert blocked_started.wait(timeout=1)
        assert not threading.Event().wait(timeout=0.06)
        transport._request_lock.release()
        request_lock_held = False
        assert blocked_done.wait(timeout=0.3)
        blocked_worker.join(timeout=1)

        assert blocked_result == {"error": "acp_timeout"}
        assert time.monotonic() - blocked_start_time < 0.24
        assert len(write_attempt_deadlines) == 2
        assert all(deadline is not None for deadline in write_attempt_deadlines)

        result = transport.request("fixture/recovered", {}, timeout=1)
        assert result == {"ready": True}
        assert json.loads(peer_events.read_text()) == [
            {
                "kind": "server_response",
                "message": {
                    "jsonrpc": "2.0",
                    "id": "permission-deferred",
                    "result": {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": "reject-once",
                        }
                    },
                },
            },
            {"kind": "client_request", "method": "fixture/recovered"},
        ]
        assert len(write_attempt_deadlines) == 3
    finally:
        if request_lock_held:
            transport._request_lock.release()
        first_worker.join(timeout=1)
        if blocked_worker is not None:
            blocked_worker.join(timeout=1)
        transport.terminate(grace_seconds=0.2)
        transport.close()


def test_partial_server_response_write_poisoned_without_replay(tmp_path, monkeypatch):
    partial_input = tmp_path / "partial-server-response.bin"
    second_input = tmp_path / "second-input.bin"
    peer = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "json.loads(sys.stdin.buffer.readline())\n"
        + _permission_request_source("permission-partial")
        + "partial=sys.stdin.buffer.readline()\n"
        + f"Path({str(partial_input)!r}).write_bytes(partial)\n"
        + "second=sys.stdin.buffer.readline()\n"
        + f"Path({str(second_input)!r}).write_bytes(second)\n"
    )
    transport = _spawn(tmp_path, peer)
    original_write = transport._write
    actual_os_write = os.write
    partial_written = False

    class ReadyThenBlockedSelector:
        def __init__(self):
            self.ready = None
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def register(self, fileobj, events):
            self.ready = (fileobj, events)

        def select(self, _timeout=None):
            self.calls += 1
            return [self.ready] if self.calls == 1 else []

    def controlled_write(message, **kwargs):
        nonlocal partial_written
        if message.get("id") != "permission-partial":
            return original_write(message, **kwargs)

        def write_one_byte(fd, data):
            nonlocal partial_written
            assert fd == transport._stdin.fileno()
            assert not partial_written
            partial_written = True
            return actual_os_write(fd, data[:1])

        with monkeypatch.context() as scoped:
            scoped.setattr(acp_transport.os, "write", write_one_byte)
            scoped.setattr(
                acp_transport.selectors,
                "DefaultSelector",
                ReadyThenBlockedSelector,
            )
            return original_write(message, **kwargs)

    monkeypatch.setattr(transport, "_write", controlled_write)

    try:
        with pytest.raises(ACPTransportError) as error:
            transport.request("fixture/partial", {}, timeout=1)
        assert error.value.reason == "acp_timeout"
        assert partial_written
        assert transport._broken

        partial_deadline = time.monotonic() + 1
        while not second_input.exists() and time.monotonic() < partial_deadline:
            time.sleep(0.005)
        assert partial_input.read_bytes() == b"{"
        assert second_input.read_bytes() == b""
        assert len(getattr(transport, "_pending_server_requests", ())) == 1

        try:
            transport.read_available(timeout=0)
        except ACPTransportError:
            pass
        with pytest.raises(ACPTransportError) as unusable:
            transport.request("fixture/after-partial", {}, timeout=0.1)
        assert unusable.value.reason == "acp_transport_unusable"
        assert len(getattr(transport, "_pending_server_requests", ())) == 1
        assert partial_input.read_bytes() == b"{"
        assert second_input.read_bytes() == b""
    finally:
        transport.terminate(grace_seconds=0.2)
        transport.close()


def _wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    return path.exists()


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


@pytest.mark.skipif(os.name != "posix", reason="requires process groups and fork")
@pytest.mark.parametrize("lookup", ["process_group", "start_identity"])
def test_identity_lookup_race_keeps_surviving_group_unknown(
    tmp_path, monkeypatch, lookup
):
    ready = tmp_path / "race-descendant-ready"
    release = tmp_path / "race-descendant-release"
    exited = tmp_path / "race-descendant-exited"
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
    lookup_entered = threading.Event()
    continue_lookup = threading.Event()
    observed = []
    worker = None

    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ready.exists()
        assert transport.process.poll() is None

        with monkeypatch.context() as scoped:
            if lookup == "process_group":
                real_getpgid = os.getpgid

                def paused_getpgid(pid):
                    if pid == transport.identity.pid:
                        lookup_entered.set()
                        assert continue_lookup.wait(timeout=2)
                        raise ProcessLookupError
                    return real_getpgid(pid)

                scoped.setattr(acp_transport.os, "getpgid", paused_getpgid)
            else:

                def paused_start_identity(pid):
                    if pid == transport.identity.pid:
                        lookup_entered.set()
                        assert continue_lookup.wait(timeout=2)
                        raise ProcessLookupError
                    return acp_transport._process_start_identity(pid)

                scoped.setattr(
                    acp_transport, "_process_start_identity", paused_start_identity
                )

            worker = threading.Thread(
                target=lambda: observed.append(
                    transport.identity_state(transport.identity)
                ),
                daemon=True,
            )
            worker.start()
            assert lookup_entered.wait(timeout=1)

            transport.process.terminate()
            transport.process.wait(timeout=1)
            assert acp_transport._process_group_exists(group_id) is True

            continue_lookup.set()
            worker.join(timeout=1)
            assert not worker.is_alive()
            assert observed == [NativeProcessState.UNKNOWN]

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
        continue_lookup.set()
        release.touch()
        if worker is not None:
            worker.join(timeout=1)
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
