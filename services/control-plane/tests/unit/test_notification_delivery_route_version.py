from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sidera_control_plane.api.schemas import NotificationDeliveryResponse
from sidera_control_plane.application.notification_queries import (
    NotificationDeliveryView,
)
from sidera_control_plane.persistence.notification_query_store import _delivery_view

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def delivery_rows(*, route_version: int) -> tuple[SimpleNamespace, SimpleNamespace]:
    route_id = uuid4()
    intent = SimpleNamespace(
        id=uuid4(),
        notification_route_id=route_id,
        contract_version=1,
        status="exhausted",
        attempts=3,
        next_attempt_at=NOW,
        last_error_code="timeout",
        delivered_at=None,
        exhausted_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    route = SimpleNamespace(
        id=route_id,
        channel_type="akashic_private",
        version=route_version,
        source_ref="synthetic-private-source-ref",
        credential_ref="synthetic-private-credential-ref",
    )
    return intent, route


def test_delivery_exposes_current_joined_route_version() -> None:
    intent, route = delivery_rows(route_version=3)

    view = _delivery_view(intent, route)
    response = NotificationDeliveryResponse.from_view(view)

    assert response.route_version == 3
    assert view.route_version == 3
    assert response.contract_version == 1
    assert response.route_id == intent.notification_route_id
    assert response.status == "exhausted"
    body = response.model_dump(mode="json")
    assert body["route_version"] == 3
    assert "source_ref" not in body
    assert "credential_ref" not in body
    assert route.source_ref not in response.model_dump_json()
    assert route.credential_ref not in response.model_dump_json()


def test_legacy_positional_delivery_view_defaults_route_version_to_none() -> None:
    intent, route = delivery_rows(route_version=3)
    view = NotificationDeliveryView(
        intent.id,
        route.id,
        route.channel_type,
        intent.contract_version,
        intent.status,
        intent.attempts,
        intent.next_attempt_at,
        intent.last_error_code,
        intent.delivered_at,
        intent.exhausted_at,
        intent.created_at,
        intent.updated_at,
    )

    response = NotificationDeliveryResponse.from_view(view)

    assert view.route_version is None
    assert response.route_version is None
    assert response.model_dump(mode="json")["route_version"] is None
    assert response.contract_version == 1
    assert response.updated_at == NOW


def test_delivery_response_defaults_missing_route_version_to_none() -> None:
    intent, route = delivery_rows(route_version=3)
    body = NotificationDeliveryResponse.from_view(
        _delivery_view(intent, route)
    ).model_dump(mode="json")
    del body["route_version"]

    response = NotificationDeliveryResponse.model_validate(body)

    assert response.route_version is None
    assert response.contract_version == 1
    assert response.route_id == route.id


@pytest.mark.parametrize(
    "invalid_version",
    (0, -1, float("nan"), float("inf"), float("-inf")),
    ids=("zero", "negative", "nan", "positive-infinity", "negative-infinity"),
)
def test_delivery_response_rejects_invalid_route_version(
    invalid_version: float,
) -> None:
    intent, route = delivery_rows(route_version=3)
    route.version = invalid_version

    with pytest.raises(ValidationError) as error:
        NotificationDeliveryResponse.from_view(_delivery_view(intent, route))

    assert error.value.errors()[0]["loc"] == ("route_version",)


@pytest.mark.parametrize("private_field", ("source_ref", "credential_ref"))
def test_delivery_response_forbids_private_route_fields(private_field: str) -> None:
    intent, route = delivery_rows(route_version=3)
    body = NotificationDeliveryResponse.from_view(
        _delivery_view(intent, route)
    ).model_dump(mode="json")
    body[private_field] = getattr(route, private_field)

    with pytest.raises(ValidationError) as error:
        NotificationDeliveryResponse.model_validate(body)

    assert error.value.errors()[0]["loc"] == (private_field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"
