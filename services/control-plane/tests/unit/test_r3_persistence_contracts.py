from sidera_control_plane.persistence.models import Base


def test_r3_persistence_models_are_registered() -> None:
    expected = {
        "module_definitions",
        "module_instances",
        "execution_bindings",
        "gateway_operations",
        "execution_workspaces",
        "process_descriptors",
        "execution_events",
        "artifacts",
        "evidence_claims",
        "execution_inputs",
        "planning_profiles",
        "planning_requests",
        "planning_brief_revisions",
        "planning_runs",
        "planning_run_leases",
        "planning_clarifications",
        "planning_proposal_revisions",
    }

    assert expected <= set(Base.metadata.tables)
