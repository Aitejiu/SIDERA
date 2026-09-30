import os

import pytest

from sidera_control_plane.demo.test_runner import (
    validate_interview_test_context,
)

pytest_plugins = ("tests.integration.result_integration_scenario",)


def pytest_collection_modifyitems(session, config, items) -> None:
    del session, config
    interview_items = [
        item
        for item in items
        if item.path.name.startswith("test_interview_")
    ]
    if not interview_items:
        return
    try:
        validate_interview_test_context(os.environ)
    except Exception as exc:
        raise pytest.UsageError("interview_test_database_refused") from exc


@pytest.fixture(autouse=True)
def _clean_database_between_integration_tests(clean_database: None) -> None:
    """Keep database isolation local to the integration test subtree."""
