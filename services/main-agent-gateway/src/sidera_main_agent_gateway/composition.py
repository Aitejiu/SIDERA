from __future__ import annotations

from importlib.util import find_spec

from sidera_runtime_guard import RuntimeBudgetGuard

from sidera_main_agent_gateway.adapter_registry import (
    MainAgentAdapterRegistration,
    select_main_agent_adapter,
)
from sidera_main_agent_gateway.adapters.unavailable import (
    UnavailableMainAgentAdapter,
)
from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.contracts import MainAgentAdapter


def _build_unconfigured(
    _settings: Settings,
    _acceptance_guard: RuntimeBudgetGuard | None,
) -> MainAgentAdapter:
    return UnavailableMainAgentAdapter("main_agent_not_configured")


def _build_akashic(
    settings: Settings,
    acceptance_guard: RuntimeBudgetGuard | None,
) -> MainAgentAdapter:
    if find_spec("akashic_sdk") is None:
        return UnavailableMainAgentAdapter("adapter_not_installed")

    from sidera_main_agent_gateway.adapters.akashic import AkashicAdapter

    if acceptance_guard is None:
        return AkashicAdapter(settings)
    return AkashicAdapter(settings, acceptance_guard=acceptance_guard)


def _build_demo(
    settings: Settings,
    acceptance_guard: RuntimeBudgetGuard | None,
) -> MainAgentAdapter:
    if find_spec("sidera_task_bridge") is None:
        return UnavailableMainAgentAdapter("adapter_not_installed")

    from sidera_main_agent_gateway.adapters.demo import DemoMainAgentAdapter

    if acceptance_guard is None:
        return DemoMainAgentAdapter(settings)
    return DemoMainAgentAdapter(settings, acceptance_guard=acceptance_guard)


def build_main_agent_adapter(
    settings: Settings,
    *,
    acceptance_guard: RuntimeBudgetGuard | None = None,
) -> MainAgentAdapter:
    registrations = {
        "akashic": MainAgentAdapterRegistration("akashic", _build_akashic),
        "interview-demo": MainAgentAdapterRegistration(
            "interview-demo", _build_demo
        ),
        "unconfigured": MainAgentAdapterRegistration(
            "unconfigured", _build_unconfigured
        ),
    }
    return select_main_agent_adapter(
        settings,
        registrations,
        acceptance_guard=acceptance_guard,
    )
