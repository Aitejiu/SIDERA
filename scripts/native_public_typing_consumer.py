from uuid import UUID

from fastapi import FastAPI
from sidera_agents import AgentCatalog, AgentExecution, AgentObservations, ProfileRef
from sidera_agents.bootstrap import create_harness_registry, open_memory_store
from sidera_agents.catalog import AgentStore
from sidera_agents.harnesses.registry import HarnessRegistry
from sidera_agents.ports import ContextSourceReader
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneRuntime,
    create_control_plane_runtime,
)
from sidera_control_plane.config import Settings
from sidera_control_plane_client import ControlPlaneClient, ControlPlaneClientConfig
from sidera_model_connections.contracts import CredentialRef
from sidera_product import ProductRuntime, ProductSettings, create_app
from sidera_runtime_guard import RuntimeRouteBinding


def product_application(settings: ProductSettings, runtime: ProductRuntime) -> FastAPI:
    return create_app(settings, runtime=runtime)


def memory_agent_store() -> AgentStore:
    return open_memory_store()


def empty_registry() -> HarnessRegistry:
    return create_harness_registry([])


def saved_profile_revision(ref: ProfileRef) -> int:
    return ref.revision


def control_plane_runtime(
    settings: Settings,
    execution: AgentExecution,
    catalog: AgentCatalog,
    observations: AgentObservations,
    sources: ContextSourceReader,
) -> ControlPlaneRuntime:
    return create_control_plane_runtime(
        settings, execution, catalog, observations, sources
    )


def public_client(config: ControlPlaneClientConfig) -> ControlPlaneClient:
    return ControlPlaneClient(config)


def credential_identity(ref: CredentialRef) -> UUID:
    return ref.credential_id


def route_identity(binding: RuntimeRouteBinding) -> str:
    return binding.route_id
