from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.context_packages import (
    ContextAcknowledgement,
    ContextConsumerType,
)
from sidera_control_plane.persistence.context_package_repository import (
    ContextAssemblyRepository,
    ContextPackageRepository,
)
from sidera_control_plane.persistence.repositories import EventRepository
from tests.integration.test_context_package_repository import (
    _event_context,
    _manifest,
    _request,
    _seed_context,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_context_package_ack_lookup_is_exactly_scoped(
    database, workspace_id: UUID
) -> None:
    request_id, revision_id, _, module_id = _seed_context(database, workspace_id)
    consumer_id = uuid4()
    request = _request(
        workspace_id=workspace_id,
        revision_id=revision_id,
        consumer_id=consumer_id,
        request_id=uuid4(),
    )
    with database.session() as session:
        assembly = ContextAssemblyRepository(session, EventRepository(session))
        assembly.add(request)
        session.flush()
        claimed = assembly.claim_next(
            workspace_id=workspace_id,
            owner="context-package-test",
            now=NOW,
            lease_ttl=timedelta(minutes=1),
            limit=1,
        )[0]
        manifest = _manifest(
            workspace_id=workspace_id,
            revision_id=revision_id,
            consumer_id=consumer_id,
            request_id=request_id,
        )
        assembly.complete_with_package(
            claimed,
            manifest,
            owner="context-package-test",
            expected_version=claimed.version,
            **_event_context(session, workspace_id),
        )
        acknowledgement = ContextAcknowledgement(
            package_id=manifest.package_id,
            package_hash=manifest.package_hash,
            consumer_type=ContextConsumerType.PLANNER,
            consumer_id=consumer_id,
            context_version=None,
            adapter_module_id=module_id,
            adapter_config_version=1,
            acknowledged_at=NOW,
        )
        repository = ContextPackageRepository(session, EventRepository(session))
        repository.add_ack(
            acknowledgement,
            **_event_context(session, workspace_id),
        )
        session.commit()

    with database.session() as session:
        repository = ContextPackageRepository(session)
        assert (
            repository.get_ack(
                manifest.package_id,
                ContextConsumerType.PLANNER,
                consumer_id,
                workspace_id,
            )
            == acknowledgement
        )
        assert (
            repository.get_ack(
                manifest.package_id,
                ContextConsumerType.PLANNER,
                uuid4(),
                workspace_id,
            )
            is None
        )
