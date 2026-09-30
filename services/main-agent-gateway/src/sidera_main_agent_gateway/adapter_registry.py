from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sidera_runtime_guard import RuntimeBudgetGuard

from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.contracts import MainAgentAdapter
from sidera_main_agent_gateway.errors import AdapterConfigurationUnavailable

type MainAgentAdapterFactory = Callable[
    [Settings, RuntimeBudgetGuard | None], MainAgentAdapter
]


@dataclass(frozen=True, slots=True)
class MainAgentAdapterRegistration:
    kind: str
    factory: MainAgentAdapterFactory


def select_main_agent_adapter(
    settings: Settings,
    registrations: Mapping[str, MainAgentAdapterRegistration],
    *,
    acceptance_guard: RuntimeBudgetGuard | None = None,
) -> MainAgentAdapter:
    registration = registrations.get(settings.adapter_kind)
    if registration is None:
        raise AdapterConfigurationUnavailable("adapter_not_registered")
    if registration.kind != settings.adapter_kind:
        raise AdapterConfigurationUnavailable("adapter_registration_mismatch")
    return registration.factory(settings, acceptance_guard)
