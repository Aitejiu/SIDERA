"""Shared database and result-integration fixtures for database-backed E2E tests."""

pytest_plugins = (
    "tests.integration.database_fixtures",
    "tests.integration.result_integration_scenario",
)
