"""Project CP task taxonomy onto strict public native capability requirements."""

from collections.abc import Collection

# Business labels stay in CP task/pool facts, not native Harness attestations.
_TASK_LABELS = frozenset({"code", "python"})


def runtime_capability_requirements(
    task_requirements: Collection[str],
) -> tuple[str, ...]:
    return tuple(sorted(set(task_requirements) - _TASK_LABELS))
