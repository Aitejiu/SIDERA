from dataclasses import dataclass
from uuid import UUID

from sidera_control_plane.domain.errors import ValidationError


@dataclass(slots=True)
class WorkerRuntimeConfiguration:
    """The exact module pair a Worker uses for newly assigned executions."""

    worker_id: UUID
    workspace_id: UUID
    adapter_module_instance_id: UUID
    adapter_module_config_version: int
    workspace_module_instance_id: UUID
    workspace_module_config_version: int
    version: int

    @property
    def runtime_identity(self) -> tuple[UUID, int, UUID, int]:
        return (
            self.adapter_module_instance_id,
            self.adapter_module_config_version,
            self.workspace_module_instance_id,
            self.workspace_module_config_version,
        )

    @classmethod
    def create(
        cls,
        *,
        worker_id: UUID,
        workspace_id: UUID,
        adapter_module_instance_id: UUID,
        adapter_module_config_version: int,
        workspace_module_instance_id: UUID,
        workspace_module_config_version: int,
    ) -> "WorkerRuntimeConfiguration":
        _require_positive_versions(
            adapter_module_config_version,
            workspace_module_config_version,
        )
        return cls(
            worker_id=worker_id,
            workspace_id=workspace_id,
            adapter_module_instance_id=adapter_module_instance_id,
            adapter_module_config_version=adapter_module_config_version,
            workspace_module_instance_id=workspace_module_instance_id,
            workspace_module_config_version=workspace_module_config_version,
            version=1,
        )

    def reconfigure(
        self,
        *,
        adapter_module_instance_id: UUID,
        adapter_module_config_version: int,
        workspace_module_instance_id: UUID,
        workspace_module_config_version: int,
    ) -> None:
        _require_positive_versions(
            adapter_module_config_version,
            workspace_module_config_version,
        )
        next_runtime = (
            adapter_module_instance_id,
            adapter_module_config_version,
            workspace_module_instance_id,
            workspace_module_config_version,
        )
        if next_runtime == self.runtime_identity:
            raise ValidationError("runtime configuration is unchanged")
        (
            self.adapter_module_instance_id,
            self.adapter_module_config_version,
            self.workspace_module_instance_id,
            self.workspace_module_config_version,
        ) = next_runtime
        self.version += 1


def _require_positive_versions(*versions: int) -> None:
    if any(
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        for version in versions
    ):
        raise ValidationError("module config versions must be positive integers")
