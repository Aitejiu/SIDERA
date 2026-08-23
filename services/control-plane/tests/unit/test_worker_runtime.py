from uuid import UUID

import pytest

from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.worker_runtime import WorkerRuntimeConfiguration

WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("21000000-0000-0000-0000-000000000001")
ADAPTER_V1_ID = UUID("22000000-0000-0000-0000-000000000001")
ADAPTER_V2_ID = UUID("22000000-0000-0000-0000-000000000002")
WORKSPACE_MODULE_V1_ID = UUID("23000000-0000-0000-0000-000000000001")
WORKSPACE_MODULE_V2_ID = UUID("23000000-0000-0000-0000-000000000002")


def make_configuration() -> WorkerRuntimeConfiguration:
    return WorkerRuntimeConfiguration.create(
        worker_id=WORKER_ID,
        workspace_id=WORKSPACE_ID,
        adapter_module_instance_id=ADAPTER_V1_ID,
        adapter_module_config_version=1,
        workspace_module_instance_id=WORKSPACE_MODULE_V1_ID,
        workspace_module_config_version=1,
    )


def test_worker_runtime_configuration_starts_at_version_one() -> None:
    configuration = make_configuration()

    assert configuration.worker_id == WORKER_ID
    assert configuration.workspace_id == WORKSPACE_ID
    assert configuration.version == 1
    assert configuration.runtime_identity == (
        ADAPTER_V1_ID,
        1,
        WORKSPACE_MODULE_V1_ID,
        1,
    )


def test_reconfigure_updates_the_complete_runtime_and_increments_version() -> None:
    configuration = make_configuration()

    configuration.reconfigure(
        adapter_module_instance_id=ADAPTER_V2_ID,
        adapter_module_config_version=2,
        workspace_module_instance_id=WORKSPACE_MODULE_V2_ID,
        workspace_module_config_version=4,
    )

    assert configuration.version == 2
    assert configuration.worker_id == WORKER_ID
    assert configuration.workspace_id == WORKSPACE_ID
    assert configuration.runtime_identity == (
        ADAPTER_V2_ID,
        2,
        WORKSPACE_MODULE_V2_ID,
        4,
    )


def test_reconfigure_rejects_an_unchanged_runtime() -> None:
    configuration = make_configuration()

    with pytest.raises(ValidationError, match="unchanged"):
        configuration.reconfigure(
            adapter_module_instance_id=ADAPTER_V1_ID,
            adapter_module_config_version=1,
            workspace_module_instance_id=WORKSPACE_MODULE_V1_ID,
            workspace_module_config_version=1,
        )


@pytest.mark.parametrize(
    "field_name",
    ("adapter_module_config_version", "workspace_module_config_version"),
)
def test_worker_runtime_configuration_rejects_non_positive_module_versions(
    field_name: str,
) -> None:
    values = {
        "adapter_module_config_version": 1,
        "workspace_module_config_version": 1,
    }
    values[field_name] = 0

    with pytest.raises(ValidationError, match="positive"):
        WorkerRuntimeConfiguration.create(
            worker_id=WORKER_ID,
            workspace_id=WORKSPACE_ID,
            adapter_module_instance_id=ADAPTER_V1_ID,
            adapter_module_config_version=values["adapter_module_config_version"],
            workspace_module_instance_id=WORKSPACE_MODULE_V1_ID,
            workspace_module_config_version=values[
                "workspace_module_config_version"
            ],
        )
