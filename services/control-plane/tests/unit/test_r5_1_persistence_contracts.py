from sidera_control_plane.persistence.models import Base

PLANNING_TABLES = {
    "planning_profiles",
    "planning_requests",
    "planning_brief_revisions",
    "planning_runs",
    "planning_run_leases",
    "planning_clarifications",
    "planning_proposal_revisions",
}


def test_r5_1_planning_tables_are_registered() -> None:
    assert PLANNING_TABLES <= set(Base.metadata.tables)


def test_r5_1_planning_models_keep_immutable_payloads_explicit() -> None:
    immutable_tables = {
        "planning_profiles",
        "planning_brief_revisions",
        "planning_proposal_revisions",
    }

    for table_name in immutable_tables:
        table = Base.metadata.tables[table_name]
        assert "created_at" in table.c
        assert "updated_at" not in table.c


def test_r5_1_planning_models_have_workspace_identity_and_revision_columns() -> None:
    for table_name in PLANNING_TABLES:
        table = Base.metadata.tables[table_name]
        assert "workspace_id" in table.c

    request_columns = set(Base.metadata.tables["planning_requests"].c.keys())
    assert {
        "current_brief_revision",
        "current_proposal_revision",
        "profile_snapshot",
    } <= request_columns
    run_columns = set(Base.metadata.tables["planning_runs"].c.keys())
    assert {"attempt", "operation_id", "capability_snapshot", "usage"} <= run_columns


def test_r5_1_materialization_and_activation_tables_are_registered() -> None:
    expected = {
        "proposal_materializations",
        "execution_requests",
        "execution_request_dependencies",
    }

    assert expected <= set(Base.metadata.tables)
    assert "proposal_revision_id" in Base.metadata.tables["execution_requests"].c
    assert "execution_lineage_id" in Base.metadata.tables["execution_requests"].c
