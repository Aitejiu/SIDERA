"""Keep local basic-stack workers schedulable."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid5

import httpx

_HEARTBEAT_SECONDS = 15.0
_MAX_HEARTBEAT_COMMAND_ATTEMPTS = 3
_HEARTBEAT_RETRY_BACKOFF_SECONDS = 0.5
_WORKER_NAME_PREFIX = "R6 basic "


class HeartbeatClient(Protocol):
    def get_workers(self, workspace_id: UUID) -> object: ...

    def command(self, envelope: dict[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class RegisteredWorker:
    id: UUID
    workspace_id: UUID
    display_name: str
    version: int


def heartbeat_registered_workers(
    client: HeartbeatClient,
    *,
    workspace_id: UUID,
    run_id: UUID,
    cycle: int | None = None,
) -> tuple[UUID, ...]:
    """Heartbeat only workers belonging to this acceptance run.

    The command identity is stable for a run/worker/version/cycle. Including the
    observed version prevents a restart from reusing an idempotency key with a
    different expected_version after a heartbeat already advanced the worker.
    """

    workers = _load_registered_workers(client, workspace_id=workspace_id, run_id=run_id)
    cycle_value = int(time.time() // _HEARTBEAT_SECONDS) if cycle is None else cycle
    reported: list[UUID] = []
    for initial_worker in workers:
        worker = initial_worker
        for attempt in range(_MAX_HEARTBEAT_COMMAND_ATTEMPTS):
            command_id = uuid5(
                run_id, f"heartbeat:{worker.id}:{worker.version}:{cycle_value}"
            )
            envelope: dict[str, object] = {
                "command_id": str(command_id),
                "workspace_id": str(workspace_id),
                "command_type": "HeartbeatWorker",
                "target_type": "worker",
                "actor": {"type": "local", "id": "aitejiu"},
                "target_id": str(worker.id),
                "expected_version": worker.version,
                "idempotency_key": str(command_id),
                "correlation_id": str(command_id),
                "payload": {"healthy": True},
            }
            try:
                result = client.command(envelope)
            except httpx.HTTPStatusError as exc:
                if (
                    not _is_version_conflict(exc)
                    or attempt + 1 >= _MAX_HEARTBEAT_COMMAND_ATTEMPTS
                ):
                    raise
                refreshed_workers = _load_registered_workers(
                    client, workspace_id=workspace_id, run_id=run_id
                )
                refreshed_worker = next(
                    (
                        candidate
                        for candidate in refreshed_workers
                        if candidate.id == worker.id
                        and candidate.display_name == worker.display_name
                    ),
                    None,
                )
                if (
                    refreshed_worker is None
                    or refreshed_worker.version <= worker.version
                ):
                    raise
                worker = refreshed_worker
                continue
            except httpx.TransportError:
                if attempt + 1 >= _MAX_HEARTBEAT_COMMAND_ATTEMPTS:
                    raise
                _wait_before_retry(attempt)
                continue
            if result.get("command_id") != str(command_id):
                raise RuntimeError("heartbeat_ack_identity_mismatch")
            break
        reported.append(worker.id)
    return tuple(reported)


def _load_registered_workers(
    client: HeartbeatClient, *, workspace_id: UUID, run_id: UUID
) -> tuple[RegisteredWorker, ...]:
    for attempt in range(_MAX_HEARTBEAT_COMMAND_ATTEMPTS):
        try:
            return _registered_workers(
                client.get_workers(workspace_id),
                workspace_id=workspace_id,
                run_id=run_id,
            )
        except httpx.TransportError:
            if attempt + 1 >= _MAX_HEARTBEAT_COMMAND_ATTEMPTS:
                raise
            _wait_before_retry(attempt)
    raise RuntimeError("worker_read_retry_exhausted")


def _wait_before_retry(attempt: int) -> None:
    time.sleep(_HEARTBEAT_RETRY_BACKOFF_SECONDS * (2**attempt))


def _is_version_conflict(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 409:
        return False
    try:
        body = exc.response.json()
    except ValueError:
        return False
    if not isinstance(body, Mapping):
        return False
    error = body.get("error")
    return isinstance(error, Mapping) and error.get("code") == "version_conflict"


class BasicAcceptanceHttpClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )

    def get_workers(self, workspace_id: UUID) -> object:
        response = self._client.get(
            f"{self._base_url}/api/v1/workers",
            params={"workspace_id": str(workspace_id)},
        )
        response.raise_for_status()
        return response.json()

    def command(self, envelope: dict[str, object]) -> Mapping[str, object]:
        response = self._client.post(
            f"{self._base_url}/api/v1/commands",
            json=envelope,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise TypeError("heartbeat_command_response_invalid")
        return value

    def close(self) -> None:
        self._client.close()


def main() -> int:
    try:
        component = _component_name()
        workspace_id = _required_uuid("SIDERA_R6_BASIC_WORKSPACE_ID")
        run_id = _required_uuid("SIDERA_R6_BASIC_RUN_ID")
        base_url = _required_text("SIDERA_CONTROL_PLANE_URL")
        interval = _interval_seconds()
        client = BasicAcceptanceHttpClient(base_url)
        try:
            reported = heartbeat_registered_workers(
                client,
                workspace_id=workspace_id,
                run_id=run_id,
            )
            _emit(component, "ready", len(reported))
            while True:
                time.sleep(interval)
                try:
                    reported = heartbeat_registered_workers(
                        client,
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                except httpx.TransportError as exc:
                    _emit(
                        component,
                        "degraded",
                        len(reported),
                        diagnostic=type(exc).__name__,
                    )
                    continue
                _emit(component, "ok", len(reported))
        finally:
            client.close()
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        httpx.HTTPError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "component": "basic-acceptance-heartbeat",
                    "status": "error",
                    "diagnostic": type(exc).__name__,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 1


def _registered_workers(
    value: object,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> tuple[RegisteredWorker, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("worker_list_invalid")
    prefix = _WORKER_NAME_PREFIX
    run_marker = run_id.hex[:8]
    result: list[RegisteredWorker] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("worker_entry_invalid")
        try:
            worker_id = UUID(str(raw["id"]))
            worker_workspace_id = UUID(str(raw["workspace_id"]))
            display_name = str(raw["display_name"])
            version = raw["version"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("worker_entry_invalid") from exc
        if worker_workspace_id != workspace_id:
            if display_name.startswith(prefix) and run_marker in display_name:
                raise ValueError("worker_workspace_mismatch")
            continue
        if (
            not display_name.startswith(prefix)
            or not display_name.endswith(f" {run_marker}")
            or display_name
            not in {
                f"R6 basic worker worker {run_marker}",
                f"R6 basic second worker worker {run_marker}",
                f"R6 basic verification worker {run_marker}",
            }
        ):
            continue
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("worker_version_invalid")
        result.append(
            RegisteredWorker(
                id=worker_id,
                workspace_id=worker_workspace_id,
                display_name=display_name,
                version=version,
            )
        )
    expected_names = {
        f"R6 basic worker worker {run_marker}",
        f"R6 basic second worker worker {run_marker}",
    }
    actual_names = {item.display_name for item in result}
    if not expected_names.issubset(actual_names) or len(
        {item.id for item in result}
    ) != len(result):
        raise ValueError("basic_acceptance_workers_incomplete")
    order = {
        f"R6 basic worker worker {run_marker}": 0,
        f"R6 basic second worker worker {run_marker}": 1,
        f"R6 basic verification worker {run_marker}": 2,
    }
    return tuple(sorted(result, key=lambda item: order[item.display_name]))


def _required_text(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name.lower()}_required")
    return value


def _required_uuid(name: str) -> UUID:
    try:
        return UUID(_required_text(name))
    except ValueError as exc:
        raise ValueError(f"{name.lower()}_invalid") from exc


def _interval_seconds() -> float:
    raw = os.environ.get("SIDERA_R6_BASIC_HEARTBEAT_SECONDS", str(_HEARTBEAT_SECONDS))
    value = float(raw)
    if value <= 0 or value >= 45:
        raise ValueError("heartbeat_interval_out_of_range")
    return value


def _component_name() -> str:
    value = os.environ.get(
        "SIDERA_R6_BASIC_HEARTBEAT_COMPONENT",
        "basic-acceptance-heartbeat",
    ).strip()
    if value not in {"basic-acceptance-heartbeat", "basic-worker-heartbeat"}:
        raise ValueError("basic_heartbeat_component_invalid")
    return value


def _emit(
    component: str,
    status: str,
    worker_count: int,
    *,
    diagnostic: str | None = None,
) -> None:
    event: dict[str, object] = {
        "component": component,
        "status": status,
        "heartbeat": True,
        "worker_count": worker_count,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    if diagnostic is not None:
        event["diagnostic"] = diagnostic
    print(
        json.dumps(event, separators=(",", ":")),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
