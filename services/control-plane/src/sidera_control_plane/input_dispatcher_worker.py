from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sidera_control_plane.application.input_dispatcher import (
    InputDispatcher,
    InputDispatchReport,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.gateway_worker import (
    GatewayRuntime,
)
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


@dataclass(slots=True)
class InputDispatcherRuntime:
    dispatcher: InputDispatcher
    gateway_runtime: GatewayRuntime

    def close(self) -> None:
        """The shared GatewayRuntime owner closes the resources."""

        return


def build_input_dispatcher(
    settings: Settings, *, gateway_runtime: GatewayRuntime
) -> InputDispatcherRuntime:
    uow_factory = lambda: SqlAlchemyUnitOfWork(
        gateway_runtime.database,
        projection_change_retention_seconds=settings.projection_change_retention_seconds,
        before_commit=gateway_runtime.before_commit,
    )
    return InputDispatcherRuntime(
        dispatcher=InputDispatcher(
            uow_factory,
            gateway_runtime.worker_gateway,
            clock=lambda: datetime.now(UTC),
        ),
        gateway_runtime=gateway_runtime,
    )


def summary_json(result: InputDispatchReport) -> str:
    return json.dumps(
        {
            "component": "input-dispatcher",
            "status": "ok",
            "claimed": result.claimed,
            "delivered": result.delivered,
            "deferred": result.deferred,
            "blocked": result.blocked,
            "unknown": result.unknown,
            "failed": result.failed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_factory: Callable[[], Settings] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Run the SIDERA execution input dispatcher"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one input dispatch batch and exit",
    )
    args = parser.parse_args(argv)
    runtime: InputDispatcherRuntime | None = None
    try:
        settings = (settings_factory or Settings)()  # pyright: ignore[reportCallIssue]
        raise RuntimeError("gateway_owner_required")
        print(
            json.dumps(
                {"component": "input-dispatcher", "status": "ready"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        while True:
            result = runtime.dispatcher.run_once(settings.gateway_batch_size)
            print(summary_json(result), flush=True)
            if args.once:
                return 0
            time.sleep(settings.gateway_poll_interval_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "component": "input-dispatcher",
                    "status": "error",
                    "error": f"{type(exc).__name__}: input dispatcher failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
