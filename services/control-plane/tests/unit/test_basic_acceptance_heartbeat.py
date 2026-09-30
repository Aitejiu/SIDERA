from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from sidera_control_plane import basic_acceptance_heartbeat as heartbeat_module
from sidera_control_plane.basic_acceptance_heartbeat import heartbeat_registered_workers

RUN_ID = UUID("10000000-0000-0000-0000-000000000101")
WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000102")
WORKER_ID = UUID("10000000-0000-0000-0000-000000000103")
SECOND_WORKER_ID = UUID("10000000-0000-0000-0000-000000000104")
OTHER_WORKER_ID = UUID("10000000-0000-0000-0000-000000000105")
VERIFICATION_WORKER_ID = UUID("10000000-0000-0000-0000-000000000106")


class HeartbeatClient:
    def __init__(self) -> None:
        self.workers = [
            {
                "id": str(WORKER_ID),
                "workspace_id": str(WORKSPACE_ID),
                "display_name": "R6 basic worker worker 10000000",
                "version": 3,
            },
            {
                "id": str(SECOND_WORKER_ID),
                "workspace_id": str(WORKSPACE_ID),
                "display_name": "R6 basic second worker worker 10000000",
                "version": 7,
            },
            {
                "id": str(OTHER_WORKER_ID),
                "workspace_id": str(WORKSPACE_ID),
                "display_name": "unrelated worker",
                "version": 9,
            },
        ]
        self.commands: list[dict[str, object]] = []

    def get_workers(self, workspace_id: UUID) -> object:
        assert workspace_id == WORKSPACE_ID
        return list(self.workers)

    def command(self, envelope: dict[str, object]) -> dict[str, object]:
        self.commands.append(envelope)
        target_id = str(envelope["target_id"])
        worker = next(item for item in self.workers if item["id"] == target_id)
        worker["version"] = int(worker["version"]) + 1
        return {
            "command_id": envelope["command_id"],
            "aggregate_id": target_id,
            "aggregate_version": worker["version"],
        }


def _http_error(status_code: int, error_code: str | None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://control-plane/api/v1/commands")
    body = (
        {"error": {"code": error_code, "message": "rejected", "details": {}}}
        if error_code is not None
        else "not-json"
    )
    response = (
        httpx.Response(
            status_code,
            content=b"not-json",
            headers={"content-type": "application/json"},
            request=request,
        )
        if error_code is None
        else httpx.Response(status_code, json=body, request=request)
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


class ConflictingHeartbeatClient(HeartbeatClient):
    def __init__(
        self,
        *,
        conflicts: int,
        advance_version: bool = True,
        status_code: int = 409,
        error_code: str | None = "version_conflict",
        fail_refresh: bool = False,
    ) -> None:
        super().__init__()
        self.conflicts = conflicts
        self.advance_version = advance_version
        self.status_code = status_code
        self.error_code = error_code
        self.fail_refresh = fail_refresh
        self.worker_list_calls = 0
        self.primary_attempts = 0

    def get_workers(self, workspace_id: UUID) -> object:
        self.worker_list_calls += 1
        if self.fail_refresh and self.worker_list_calls > 1:
            raise OSError("worker refresh failed")
        return super().get_workers(workspace_id)

    def command(self, envelope: dict[str, object]) -> dict[str, object]:
        self.commands.append(envelope)
        target_id = str(envelope["target_id"])
        worker = next(item for item in self.workers if item["id"] == target_id)
        if target_id == str(WORKER_ID) and self.primary_attempts < self.conflicts:
            self.primary_attempts += 1
            if self.advance_version:
                worker["version"] = int(worker["version"]) + 1
            raise _http_error(self.status_code, self.error_code)
        worker["version"] = int(worker["version"]) + 1
        return {
            "command_id": envelope["command_id"],
            "aggregate_id": target_id,
            "aggregate_version": worker["version"],
        }


class TransientHeartbeatClient(HeartbeatClient):
    def __init__(self, *, worker_read_timeouts: int = 0, command_timeouts: int = 0):
        super().__init__()
        self.worker_read_timeouts = worker_read_timeouts
        self.command_timeouts = command_timeouts
        self.worker_list_calls = 0
        self.results: dict[str, dict[str, object]] = {}

    def get_workers(self, workspace_id: UUID) -> object:
        self.worker_list_calls += 1
        if self.worker_read_timeouts > 0:
            self.worker_read_timeouts -= 1
            raise httpx.ReadTimeout(
                "worker read timed out",
                request=httpx.Request("GET", "http://control-plane/api/v1/workers"),
            )
        return super().get_workers(workspace_id)

    def command(self, envelope: dict[str, object]) -> dict[str, object]:
        self.commands.append(dict(envelope))
        command_id = str(envelope["command_id"])
        result = self.results.get(command_id)
        if result is None:
            target_id = str(envelope["target_id"])
            worker = next(item for item in self.workers if item["id"] == target_id)
            worker["version"] = int(worker["version"]) + 1
            result = {
                "command_id": command_id,
                "aggregate_id": target_id,
                "aggregate_version": worker["version"],
            }
            self.results[command_id] = result
        if self.command_timeouts > 0:
            self.command_timeouts -= 1
            raise httpx.ReadTimeout(
                "command response timed out",
                request=httpx.Request("POST", "http://control-plane/api/v1/commands"),
            )
        return result


def test_heartbeat_targets_only_the_two_basic_workers() -> None:
    client = HeartbeatClient()

    reported = heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )

    assert reported == (WORKER_ID, SECOND_WORKER_ID)
    assert [item["target_id"] for item in client.commands] == [
        str(WORKER_ID),
        str(SECOND_WORKER_ID),
    ]
    assert all(item["command_type"] == "HeartbeatWorker" for item in client.commands)
    assert all(item["payload"] == {"healthy": True} for item in client.commands)


def test_heartbeat_includes_an_optional_verification_worker() -> None:
    client = HeartbeatClient()
    client.workers.append(
        {
            "id": str(VERIFICATION_WORKER_ID),
            "workspace_id": str(WORKSPACE_ID),
            "display_name": "R6 basic verification worker 10000000",
            "version": 2,
        }
    )

    reported = heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )

    assert reported == (WORKER_ID, SECOND_WORKER_ID, VERIFICATION_WORKER_ID)
    assert [item["target_id"] for item in client.commands] == [
        str(WORKER_ID),
        str(SECOND_WORKER_ID),
        str(VERIFICATION_WORKER_ID),
    ]


def test_heartbeat_command_identity_is_deterministic_for_replay() -> None:
    first = HeartbeatClient()
    second = HeartbeatClient()

    heartbeat_registered_workers(
        first,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )
    heartbeat_registered_workers(
        second,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )

    assert [item["command_id"] for item in first.commands] == [
        item["command_id"] for item in second.commands
    ]


def test_heartbeat_command_identity_changes_when_worker_version_changes_within_cycle() -> (
    None
):
    client = HeartbeatClient()

    heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )
    first_command_ids = [item["command_id"] for item in client.commands]

    heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )
    second_command_ids = [item["command_id"] for item in client.commands[2:]]

    assert first_command_ids != second_command_ids


def test_heartbeat_rejects_a_worker_from_another_workspace() -> None:
    client = HeartbeatClient()
    client.workers[0]["workspace_id"] = str(
        UUID("10000000-0000-0000-0000-000000000199")
    )

    with pytest.raises(ValueError, match="worker_workspace_mismatch"):
        heartbeat_registered_workers(
            client,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            cycle=12,
        )


def test_heartbeat_refreshes_and_retries_same_worker_after_version_conflict() -> None:
    client = ConflictingHeartbeatClient(conflicts=1)

    reported = heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )

    assert reported == (WORKER_ID, SECOND_WORKER_ID)
    assert client.worker_list_calls == 2
    assert [item["target_id"] for item in client.commands] == [
        str(WORKER_ID),
        str(WORKER_ID),
        str(SECOND_WORKER_ID),
    ]
    assert [item["expected_version"] for item in client.commands] == [3, 4, 7]
    assert client.commands[0]["command_id"] != client.commands[1]["command_id"]


@pytest.mark.parametrize(
    ("status_code", "error_code", "advance_version", "expected_worker_reads"),
    [
        (409, "idempotency_conflict", True, 1),
        (409, None, True, 1),
        (409, "version_conflict", False, 2),
        (503, "service_unavailable", True, 1),
    ],
)
def test_heartbeat_does_not_retry_non_version_conflicts(
    status_code: int,
    error_code: str | None,
    advance_version: bool,
    expected_worker_reads: int,
) -> None:
    client = ConflictingHeartbeatClient(
        conflicts=1,
        advance_version=advance_version,
        status_code=status_code,
        error_code=error_code,
    )

    with pytest.raises(httpx.HTTPStatusError):
        heartbeat_registered_workers(
            client,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            cycle=12,
        )

    assert client.worker_list_calls == expected_worker_reads
    assert len(client.commands) == 1


def test_heartbeat_stops_after_three_version_conflicts() -> None:
    client = ConflictingHeartbeatClient(conflicts=10)

    with pytest.raises(httpx.HTTPStatusError):
        heartbeat_registered_workers(
            client,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            cycle=12,
        )

    assert client.worker_list_calls == 3
    assert len(client.commands) == 3
    assert [item["target_id"] for item in client.commands] == [
        str(WORKER_ID),
        str(WORKER_ID),
        str(WORKER_ID),
    ]
    assert [item["expected_version"] for item in client.commands] == [3, 4, 5]
    assert len({item["command_id"] for item in client.commands}) == 3


def test_heartbeat_propagates_worker_refresh_failure() -> None:
    client = ConflictingHeartbeatClient(conflicts=1, fail_refresh=True)

    with pytest.raises(OSError, match="worker refresh failed"):
        heartbeat_registered_workers(
            client,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            cycle=12,
        )

    assert client.worker_list_calls == 2
    assert len(client.commands) == 1


def test_heartbeat_retries_transient_worker_read_timeouts(monkeypatch) -> None:
    client = TransientHeartbeatClient(worker_read_timeouts=2)
    delays: list[float] = []
    monkeypatch.setattr(heartbeat_module.time, "sleep", delays.append)

    reported = heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )

    assert reported == (WORKER_ID, SECOND_WORKER_ID)
    assert client.worker_list_calls == 3
    assert delays == [0.5, 1.0]


def test_heartbeat_retries_timed_out_command_with_same_idempotency_identity(
    monkeypatch,
) -> None:
    client = TransientHeartbeatClient(command_timeouts=1)
    delays: list[float] = []
    monkeypatch.setattr(heartbeat_module.time, "sleep", delays.append)

    reported = heartbeat_registered_workers(
        client,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        cycle=12,
    )

    assert reported == (WORKER_ID, SECOND_WORKER_ID)
    assert client.commands[0] == client.commands[1]
    assert client.commands[0]["command_id"] == client.commands[1]["command_id"]
    assert (
        client.commands[0]["idempotency_key"] == client.commands[1]["idempotency_key"]
    )
    assert client.workers[0]["version"] == 4
    assert delays == [0.5]


def test_heartbeat_stops_after_three_transient_command_timeouts(monkeypatch) -> None:
    client = TransientHeartbeatClient(command_timeouts=10)
    delays: list[float] = []
    monkeypatch.setattr(heartbeat_module.time, "sleep", delays.append)

    with pytest.raises(httpx.ReadTimeout):
        heartbeat_registered_workers(
            client,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            cycle=12,
        )

    assert len(client.commands) == 3
    assert len({item["command_id"] for item in client.commands}) == 1
    assert client.workers[0]["version"] == 4
    assert delays == [0.5, 1.0]


def test_heartbeat_process_recovers_on_a_later_cycle_after_transport_failure(
    monkeypatch,
) -> None:
    class StopAfterRecovery(Exception):
        pass

    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    calls = 0
    events: list[tuple[str, int, str | None]] = []

    def heartbeat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ReadTimeout(
                "control plane read timed out",
                request=httpx.Request("GET", "http://control-plane/api/v1/workers"),
            )
        return (WORKER_ID, SECOND_WORKER_ID)

    def emit(_component: str, status: str, worker_count: int, diagnostic=None) -> None:
        events.append((status, worker_count, diagnostic))
        if status == "ok":
            raise StopAfterRecovery

    monkeypatch.setenv("SIDERA_R6_BASIC_WORKSPACE_ID", str(WORKSPACE_ID))
    monkeypatch.setenv("SIDERA_R6_BASIC_RUN_ID", str(RUN_ID))
    monkeypatch.setenv("SIDERA_CONTROL_PLANE_URL", "http://control-plane")
    monkeypatch.setattr(
        heartbeat_module, "BasicAcceptanceHttpClient", lambda _url: client
    )
    monkeypatch.setattr(heartbeat_module, "heartbeat_registered_workers", heartbeat)
    monkeypatch.setattr(heartbeat_module, "_emit", emit)
    monkeypatch.setattr(heartbeat_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(StopAfterRecovery):
        heartbeat_module.main()

    assert client.closed is True
    assert events == [
        ("ready", 2, None),
        ("degraded", 2, "ReadTimeout"),
        ("ok", 2, None),
    ]
