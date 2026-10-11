"""Fresh membership of local dispatcher slots from immutable native setup."""

from collections.abc import Callable, Mapping
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select

from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.modules import (
    LOCAL_VERIFICATION_PROVIDER,
    WORKER_ADAPTER_MODULE_TYPE,
    WORKSPACE_PROVIDER_MODULE_TYPE,
    ModuleLifecycle,
)
from sidera_control_plane.domain.native_setup import (
    NATIVE_SETUP_POLICY_KEY,
    native_local_slot_ids,
)
from sidera_control_plane.domain.result_integration import VerificationBindingV1
from sidera_control_plane.domain.worker import Worker, WorkerDesiredState
from sidera_control_plane.persistence.models import PlanningProfileModel
from sidera_control_plane.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _module(uow, module_id, workspace_id, version, module_type, provider):
    instance = uow.modules.get_instance(module_id, workspace_id)
    if (
        instance is None
        or instance.id != module_id
        or instance.workspace_id != workspace_id
        or instance.config_version != version
        or instance.lifecycle is not ModuleLifecycle.ACTIVE
    ):
        return None
    definition = uow.modules.get_definition(instance.definition_id, workspace_id)
    if (
        definition is None
        or definition.id != instance.definition_id
        or definition.workspace_id != workspace_id
        or definition.lifecycle is not ModuleLifecycle.ACTIVE
        or definition.module_type != module_type
        or definition.provider != provider
    ):
        return None
    return instance


class SavedNativeWorkerSlots:
    def __init__(self, uow_factory: Callable[[], SqlAlchemyUnitOfWork]):
        self._uow_factory = uow_factory

    def owned_slots(self) -> tuple[Worker, ...]:
        with self._uow_factory() as uow:
            return self._in_uow(uow)

    def _in_uow(self, uow: SqlAlchemyUnitOfWork) -> tuple[Worker, ...]:
        owned = {}
        rows = uow.session.execute(
            select(
                PlanningProfileModel.id,
                PlanningProfileModel.workspace_id,
                PlanningProfileModel.version,
            ).where(
                PlanningProfileModel.profile_key == NATIVE_SETUP_POLICY_KEY,
                PlanningProfileModel.execution_mode == "native_profile",
            )
        )
        for policy_id, workspace_id, version in rows:
            policy = uow.planning_profiles.get(policy_id, workspace_id, version=version)
            try:
                if (
                    policy is None
                    or policy.profile_key != NATIVE_SETUP_POLICY_KEY
                    or policy.execution_mode != "native_profile"
                ):
                    continue
                metadata = policy.system_policy["local_execution_slots"]
                if (
                    not isinstance(metadata, Mapping)
                    or type(metadata.get("schema_version")) is not int
                    or metadata["schema_version"] != 1
                ):
                    continue
                result_integration = policy.system_policy["result_integration"]
                if not isinstance(result_integration, Mapping):
                    continue
                binding = VerificationBindingV1.from_mapping(
                    result_integration["verification_binding"]
                )
                worker_ids = native_local_slot_ids(
                    binding.workspace_module_ref, binding.adapter_module_ref
                )
                raw_ids = (
                    metadata["agent_run_worker_id"],
                    metadata["verification_worker_id"],
                )
                if (
                    any(not isinstance(value, str) for value in raw_ids)
                    or tuple(UUID(value) for value in raw_ids) != worker_ids
                ):
                    continue
            except (KeyError, TypeError, ValueError, DomainError):
                continue
            workspace = _module(
                uow,
                binding.workspace_module_ref,
                workspace_id,
                binding.workspace_config_version,
                WORKSPACE_PROVIDER_MODULE_TYPE,
                "git-worktree",
            )
            verification = _module(
                uow,
                binding.adapter_module_ref,
                workspace_id,
                binding.adapter_config_version,
                WORKER_ADAPTER_MODULE_TYPE,
                LOCAL_VERIFICATION_PROVIDER,
            )
            agent_definition_id = uuid5(
                workspace_id, "sidera.native.definition:agent-run"
            )
            agent_id = uuid5(agent_definition_id, sha256(b"{}").hexdigest())
            agent = _module(
                uow, agent_id, workspace_id, 1, WORKER_ADAPTER_MODULE_TYPE, "agent-run"
            )
            if (
                workspace is None
                or verification is None
                or agent is None
                or agent.config
                or agent.secret_refs
            ):
                continue
            for worker_id, adapter, provider in (
                (worker_ids[0], agent, "agent-run"),
                (worker_ids[1], verification, LOCAL_VERIFICATION_PROVIDER),
            ):
                worker = uow.workers.get(worker_id, workspace_id)
                configuration = uow.worker_runtime_configurations.get_for_worker(
                    worker_id, workspace_id
                )
                if (
                    worker is None
                    or worker.id != worker_id
                    or worker.workspace_id != workspace_id
                    or worker.adapter_type != provider
                    or worker.desired_state is not WorkerDesiredState.ACTIVE
                    or configuration is None
                    or configuration.worker_id != worker_id
                    or configuration.workspace_id != workspace_id
                    or configuration.runtime_identity
                    != (
                        adapter.id,
                        adapter.config_version,
                        workspace.id,
                        workspace.config_version,
                    )
                ):
                    continue
                owned[worker_id] = worker
        return tuple(owned[worker_id] for worker_id in sorted(owned))
