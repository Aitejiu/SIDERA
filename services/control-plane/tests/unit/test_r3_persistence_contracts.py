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
    }

    assert expected <= set(Base.metadata.tables)
