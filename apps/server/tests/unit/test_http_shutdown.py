import asyncio
import socket
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from time import monotonic
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from sidera_product.app import create_app
from sidera_product.composition import build_product_runtime
from sidera_product.config import ProductSettings
from tests.support.agent_management_desktop import OwnedUvicorn, start_owned_backend


@contextmanager
def owned_backend(app: FastAPI, settings_values) -> Iterator[OwnedUvicorn]:
    resource_owner = SimpleNamespace(backend=None)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        settings = ProductSettings.model_validate(
            {
                **settings_values,
                "port": listener.getsockname()[1],
                "close_grace_seconds": 0.25,
            }
        )
        try:
            yield start_owned_backend(
                app, settings, listener=listener, resource_owner=resource_owner
            )
        finally:
            backend = resource_owner.backend
            if backend is not None:
                backend.server.should_exit = True
                backend.thread.join(timeout=5)
                listener.close()
                assert not backend.thread.is_alive(), (
                    "Loopback server did not settle after client cleanup"
                )
                assert listener.fileno() == -1
                assert backend.errors == []


def test_owned_backend_closes_without_inflight_http(
    settings_values, no_database_connections
):
    order = []

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            order.append("owner_close")

    with owned_backend(FastAPI(lifespan=lifespan), settings_values) as backend:
        assert backend.close() is True
        assert not backend.thread.is_alive()
        assert backend.listener.fileno() == -1
        assert order == ["owner_close"]
        assert backend.close() is True


def test_owned_backend_bounds_close_with_open_http_stream(
    settings_values, no_database_connections
):
    entered = threading.Event()
    order = []
    active_http_work = 0

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            order.append("owner_close")

    async def stream():
        nonlocal active_http_work
        active_http_work += 1
        try:
            yield b"data: ready\n\n"
            entered.set()
            await asyncio.Event().wait()
        finally:
            active_http_work -= 1
            order.append("http_cancelled")

    app = FastAPI(lifespan=lifespan)

    @app.get("/stream")
    async def events():
        return StreamingResponse(stream(), media_type="text/event-stream")

    with (
        owned_backend(app, settings_values) as backend,
        httpx.Client(
            base_url=f"http://127.0.0.1:{backend.server.config.port}",
            trust_env=False,
            timeout=3,
        ) as client,
        client.stream("GET", "/stream") as response,
    ):
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines) == "data: ready"
        assert entered.wait(timeout=3)
        assert active_http_work == 1
        started = monotonic()
        # Uvicorn timeout-cancellation diagnostics are expected here.
        assert backend.close() is True
        assert monotonic() - started < 5
        assert not response.is_closed
        assert backend.server.config.timeout_graceful_shutdown == 0.25
        assert not backend.thread.is_alive()
        assert backend.listener.fileno() == -1
        assert active_http_work == 0
        assert sorted(order) == ["http_cancelled", "owner_close"]
        assert backend.close() is True


def test_product_lifespan_cancels_http_stream_before_runtime_close(
    settings_values, no_database_connections, monkeypatch
):
    entered = threading.Event()
    order = []
    active_http_work = 0
    close_observations = []
    resource_owner = SimpleNamespace(backend=None)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        settings = ProductSettings.model_validate(
            {
                **settings_values,
                "port": listener.getsockname()[1],
                "close_grace_seconds": 0.25,
            }
        )
        runtime = build_product_runtime(settings)
        original_close = runtime.close

        def observe_close(*, grace_seconds=None):
            close_observations.append(active_http_work)
            order.append("runtime_close_entered")
            return original_close(grace_seconds=grace_seconds)

        async def stream():
            nonlocal active_http_work
            active_http_work += 1
            try:
                yield b"data: ready\n\n"
                entered.set()
                await asyncio.Event().wait()
            finally:
                active_http_work -= 1
                order.append("http_cancelled")

        async def events():
            return StreamingResponse(stream(), media_type="text/event-stream")

        try:
            monkeypatch.setattr(runtime, "start", lambda: None)
            monkeypatch.setattr(runtime, "close", observe_close)
            app = create_app(runtime=runtime)
            app.router.routes.insert(
                0, APIRoute("/__test_http_drain_stream", events, methods=["GET"])
            )
            backend = start_owned_backend(
                app, settings, listener=listener, resource_owner=resource_owner
            )
            with (
                httpx.Client(
                    base_url=settings.local_url, trust_env=False, timeout=3
                ) as client,
                client.stream("GET", "/__test_http_drain_stream") as response,
            ):
                assert response.status_code == 200
                lines = response.iter_lines()
                assert next(lines) == "data: ready"
                assert entered.wait(timeout=3)
                assert active_http_work == 1
                assert close_observations == []
                assert runtime.status()["execution"]["ready"] is False
                started = monotonic()
                # Uvicorn timeout-cancellation diagnostics are expected here.
                assert backend.close() is True
                assert monotonic() - started < 5
                assert not response.is_closed
                assert backend.server.config.timeout_graceful_shutdown == 0.25
                assert not backend.thread.is_alive()
                assert backend.listener.fileno() == -1
                assert active_http_work == 0
                assert order.index("http_cancelled") < order.index(
                    "runtime_close_entered"
                )
                assert close_observations == [0]
                assert runtime.status()["lifecycle"] == "closed"
                assert backend.close() is True
                assert close_observations == [0]
        finally:
            backend = resource_owner.backend
            try:
                if backend is not None:
                    backend.server.should_exit = True
                    backend.thread.join(timeout=5)
                    assert not backend.thread.is_alive(), (
                        "Product HTTP server did not settle after client cleanup"
                    )
                    assert backend.errors == []
            finally:
                listener.close()
                # Cleanup fallback is outside the observed lifespan assertions.
                assert original_close() is True
