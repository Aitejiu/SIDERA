import pytest


@pytest.fixture(autouse=True)
def _clean_database_between_integration_tests(clean_database: None) -> None:
    """Keep database isolation local to the integration test subtree."""
