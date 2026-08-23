from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ModuleSummaryView:
    """Safe module metadata for diagnostics and runtime selection UI."""

    id: UUID
    instance_name: str
    provider: str
    module_type: str
    config_version: int
    definition_lifecycle: str
    instance_lifecycle: str


class ModuleQueryStore(Protocol):
    def list_modules(
        self, workspace_id: UUID, module_type: str | None = None
    ) -> tuple[ModuleSummaryView, ...]:
        raise NotImplementedError


class ModuleQueryService:
    def __init__(self, store: ModuleQueryStore) -> None:
        self._store = store

    def list_modules(
        self, workspace_id: UUID, module_type: str | None = None
    ) -> tuple[ModuleSummaryView, ...]:
        return self._store.list_modules(workspace_id, module_type)
