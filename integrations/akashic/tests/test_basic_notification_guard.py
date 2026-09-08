"""Provider-free route and delivery guards for the bounded notification gate."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from sidera_task_bridge.notification_gateway import (
    NotificationRouteGuardError,
    resolve_authorized_private_route,
)
from sidera_task_bridge.state import PrivateRoute

ROUTE_SOURCE = "src_" + "a" * 32
SIBLING_SOURCE = "src_" + "b" * 32
PRIVATE_CHAT = "local-test-chat-never-emitted"


def _routes() -> dict[str, PrivateRoute]:
    return {
        ROUTE_SOURCE: PrivateRoute(
            channel="telegram",
            chat_id=PRIVATE_CHAT,
            session_key="telegram:private:test",
        ),
        SIBLING_SOURCE: PrivateRoute(
            channel="telegram",
            chat_id="sibling-chat-never-emitted",
            session_key="telegram:private:sibling",
        ),
    }


def test_authorized_env_route_resolves_one_exact_personal_route() -> None:
    selection = resolve_authorized_private_route(
        "env:SIDERA_BASIC_NOTIFICATION_ROUTE",
        environ={"SIDERA_BASIC_NOTIFICATION_ROUTE": ROUTE_SOURCE},
        routes=_routes(),
    )

    assert selection.source_ref == ROUTE_SOURCE
    assert selection.route.channel == "telegram"
    assert selection.route.chat_id == PRIVATE_CHAT


def test_unknown_route_does_not_fall_back_to_or_enumerate_a_sibling() -> None:
    class NoEnumerationMapping(Mapping[str, PrivateRoute]):
        def __init__(self, values: dict[str, PrivateRoute]) -> None:
            self._values = values

        def __getitem__(self, key: str) -> PrivateRoute:
            return self._values[key]

        def __iter__(self):
            raise AssertionError("route guard must not enumerate contacts")

        def __len__(self) -> int:
            return len(self._values)

        def get(self, key: str, default: PrivateRoute | None = None):
            return self._values.get(key, default)

    with pytest.raises(NotificationRouteGuardError) as error:
        resolve_authorized_private_route(
            "env:SIDERA_BASIC_NOTIFICATION_ROUTE",
            environ={"SIDERA_BASIC_NOTIFICATION_ROUTE": "src_unknown"},
            routes=NoEnumerationMapping(_routes()),
        )

    assert error.value.code == "notification_route_not_found"


@pytest.mark.parametrize(
    ("route_reference", "environ", "code"),
    [
        (None, {}, "notification_route_reference_missing"),
        ("env:SIDERA_BASIC_NOTIFICATION_ROUTE", {}, "notification_route_reference_unavailable"),
        ("route:raw-value", {}, "notification_route_reference_invalid"),
    ],
)
def test_route_reference_is_fail_closed(
    route_reference: str | None,
    environ: dict[str, str],
    code: str,
) -> None:
    with pytest.raises(NotificationRouteGuardError) as error:
        resolve_authorized_private_route(
            route_reference,
            environ=environ,
            routes=_routes(),
        )
    assert error.value.code == code


def test_module_route_is_explicit_and_non_telegram_routes_are_rejected() -> None:
    with pytest.raises(NotificationRouteGuardError, match="notification_route_channel_unsupported"):
        resolve_authorized_private_route(
            "module:personal-route",
            environ={},
            module_routes={"personal-route": "src_non_telegram"},
            routes={
                "src_non_telegram": PrivateRoute(
                    channel="email",
                    chat_id="not-a-chat",
                    session_key="email:private",
                )
            },
        )


def test_guard_does_not_place_route_or_credentials_in_exception_text() -> None:
    secret = "sk-test-route-secret"
    with pytest.raises(NotificationRouteGuardError) as error:
        resolve_authorized_private_route(
            "env:SIDERA_BASIC_NOTIFICATION_ROUTE",
            environ={"SIDERA_BASIC_NOTIFICATION_ROUTE": secret},
            routes={},
        )

    assert secret not in str(error.value)
    assert secret not in repr(error.value)


def test_guard_module_imports_remain_async_safe() -> None:
    async def scenario() -> None:
        selection = resolve_authorized_private_route(
            "module:personal-route",
            environ={},
            module_routes={"personal-route": ROUTE_SOURCE},
            routes=_routes(),
        )
        assert selection.source_ref == ROUTE_SOURCE

    asyncio.run(scenario())
