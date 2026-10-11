"""Lazy repository identities from immutable, explicitly owned native setup."""

import re
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from sidera_control_plane.adapters.git_workspace import opaque_repository_ref
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.modules import WORKSPACE_PROVIDER_MODULE_TYPE
from sidera_control_plane.domain.native_setup import NATIVE_SETUP_POLICY_KEY
from sidera_control_plane.domain.result_integration import VerificationBindingV1
from sidera_control_plane.domain.result_operations import ResultError
from sidera_control_plane.persistence.models import PlanningProfileModel


def _saved_paths(uow_factory) -> dict[str, Path]:
    paths = {}
    with uow_factory() as uow:
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
                    raise ValueError()
                result = policy.system_policy["result_integration"]
                ref = result["repository_ref"]
                if (
                    not isinstance(ref, str)
                    or re.fullmatch(r"git-repository://[0-9a-f]{64}", ref) is None
                ):
                    raise ValueError()
                binding = VerificationBindingV1.from_mapping(
                    result["verification_binding"]
                )
                instance = uow.modules.get_instance(
                    binding.workspace_module_ref, workspace_id
                )
                if (
                    instance is None
                    or instance.config_version != binding.workspace_config_version
                ):
                    raise ValueError()
                definition = uow.modules.get_definition(
                    instance.definition_id, workspace_id
                )
                if (
                    definition is None
                    or definition.module_type != WORKSPACE_PROVIDER_MODULE_TYPE
                    or definition.provider != "git-worktree"
                ):
                    raise ValueError()
                raw = instance.config["workspace.repository_ref"]
                if not isinstance(raw, str) or not Path(raw).is_absolute():
                    raise ValueError()
                path = Path(raw)
            except (KeyError, TypeError, ValueError, DomainError):
                raise ResultError("result_repository_unconfigured", {}) from None
            if ref in paths and paths[ref] != path:
                raise ResultError("result_repository_invalid", {})
            paths[ref] = path
    return paths


class SavedNativeRepositories:
    def __init__(self, uow_factory: Callable[[], ControlPlaneUnitOfWork]):
        self._uow_factory = uow_factory

    def __call__(self, repository_ref: str) -> Path:
        if (
            not isinstance(repository_ref, str)
            or re.fullmatch(r"git-repository://[0-9a-f]{64}", repository_ref) is None
        ):
            raise ResultError("result_repository_unconfigured", {})
        path = _saved_paths(self._uow_factory).get(repository_ref)
        if path is None:
            raise ResultError("result_repository_unconfigured", {})
        try:
            if any(candidate.is_symlink() for candidate in (path, *path.parents)):
                raise OSError()
            canonical = path.resolve(strict=True)
            if not canonical.is_dir():
                raise OSError()
        except OSError:
            raise ResultError("result_repository_unavailable", {}) from None
        if canonical != path or opaque_repository_ref(canonical) != repository_ref:
            raise ResultError("result_repository_invalid", {})
        return canonical

    def repository_refs(self) -> tuple[str, ...]:
        return tuple(sorted(_saved_paths(self._uow_factory)))
