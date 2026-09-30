from uuid import UUID

import pytest


@pytest.fixture
def workspace_ids() -> tuple[UUID, UUID]:
    return UUID(int=1), UUID(int=2)
