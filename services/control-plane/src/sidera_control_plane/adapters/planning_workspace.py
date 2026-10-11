import os
import stat
from pathlib import Path
from uuid import UUID

from sidera_agents.contracts import ExecutionWorkspace
from sidera_agents.errors import AgentError


class PrivatePlanningWorkspaceProvider:
    def __init__(self, root: Path):
        self._root = root

    def prepare(
        self, *, workspace_id: UUID, planning_run_id: UUID, operation_id: UUID
    ) -> ExecutionWorkspace:
        descriptors = []
        try:
            root = self._root
            if (
                not isinstance(root, Path)
                or not root.is_absolute()
                or root == root.parent
                or ".." in root.parts
                or root.resolve() != root
                or any(
                    type(value) is not UUID
                    for value in (workspace_id, planning_run_id, operation_id)
                )
            ):
                raise ValueError("invalid owned planning directory")
            root.mkdir(mode=0o700, exist_ok=True)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(root, flags))
            path = root
            for identity in (workspace_id, planning_run_id, operation_id):
                parent = descriptors[-1]
                self._verify_private_directory(parent)
                name = str(identity)
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                descriptors.append(os.open(name, flags, dir_fd=parent))
                path = path / name
            self._verify_private_directory(descriptors[-1])
            actual = path.resolve(strict=True)
            saved = os.fstat(descriptors[-1])
            current = path.lstat()
            if actual != path or (saved.st_dev, saved.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise ValueError("owned planning directory changed")
            return ExecutionWorkspace(
                directory=str(actual), writable=True, artifact_directory=None
            )
        except (OSError, ValueError, RuntimeError):
            raise AgentError(
                "planning_workspace_unavailable", "Planning workspace is unavailable."
            ) from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _verify_private_directory(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("planning directory is not private and owned")
