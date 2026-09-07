import pytest

pytest_plugins = ("tests.integration.result_integration_scenario",)


@pytest.fixture(autouse=True)
def _clean_database_between_integration_tests(clean_database: None) -> None:
    """Keep database isolation local to the integration test subtree."""
