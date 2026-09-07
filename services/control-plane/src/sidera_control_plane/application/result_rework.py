from __future__ import annotations

from collections.abc import Iterable, Mapping
from uuid import UUID

from sidera_control_plane.domain.code_results import CodeResultSnapshotV1
from sidera_control_plane.domain.result_operations import ResultError, ReworkImpactV1

type ResultProvenance = Mapping[UUID, CodeResultSnapshotV1]
type HardResultEdge = tuple[UUID, UUID]


def compute_rework_impact(
    *,
    changed_result_id: UUID,
    replacement: CodeResultSnapshotV1,
    provenance: ResultProvenance,
    hard_edges: Iterable[HardResultEdge],
) -> ReworkImpactV1:
    """Compute a stable local impact report without mutating result history.

    A fixed consumer relation is stronger than a plan edge. A plan edge with no
    matching consumed-result fact is therefore classified as pending impact;
    it is never silently treated as reusable. Edges that point outside the
    fixed provenance are structural gaps and require a plan revision.
    """

    if not isinstance(changed_result_id, UUID):
        raise ResultError("rework_changed_result_invalid", {})
    if not isinstance(replacement, CodeResultSnapshotV1):
        raise ResultError("rework_replacement_invalid", {})
    if not isinstance(provenance, Mapping):
        raise ResultError("rework_provenance_invalid", {})

    snapshots: dict[UUID, CodeResultSnapshotV1] = {}
    for result_id, snapshot in provenance.items():
        if not isinstance(result_id, UUID) or not isinstance(snapshot, CodeResultSnapshotV1):
            raise ResultError("rework_provenance_invalid", {})
        if result_id != snapshot.id:
            raise ResultError(
                "rework_provenance_key_mismatch", {"result_id": str(result_id)}
            )
        snapshots[result_id] = snapshot

    changed = snapshots.get(changed_result_id)
    if changed is None:
        raise ResultError(
            "rework_source_result_not_found", {"result_id": str(changed_result_id)}
        )
    if replacement.id == changed_result_id:
        raise ResultError("rework_replacement_not_new", {})
    if (
        replacement.workspace_id != changed.workspace_id
        or replacement.root_task_id != changed.root_task_id
        or replacement.worker_item_id != changed.worker_item_id
        or replacement.repository_ref != changed.repository_ref
    ):
        raise ResultError(
            "rework_replacement_scope_mismatch",
            {"changed_result_id": str(changed_result_id)},
        )

    consumers_by_result: dict[UUID, set[UUID]] = {}
    workers = {snapshot.worker_item_id for snapshot in snapshots.values()}
    for snapshot in snapshots.values():
        for consumed_result_id in snapshot.consumed_result_ids:
            consumers_by_result.setdefault(consumed_result_id, set()).add(
                snapshot.worker_item_id
            )

    downstream_by_worker: dict[UUID, set[UUID]] = {}
    for edge in hard_edges:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise ResultError("rework_hard_edge_invalid", {})
        upstream, downstream = edge
        if not isinstance(upstream, UUID) or not isinstance(downstream, UUID):
            raise ResultError("rework_hard_edge_invalid", {})
        if upstream == downstream:
            raise ResultError("rework_hard_edge_invalid", {"worker_id": str(upstream)})
        downstream_by_worker.setdefault(upstream, set()).add(downstream)

    equivalent = _same_code_and_input(changed, replacement)
    changed_worker = changed.worker_item_id
    direct_consumers = consumers_by_result.get(changed_result_id, set()) - {changed_worker}
    planned_consumers = downstream_by_worker.get(changed_worker, set())

    affected: set[UUID] = set()
    reasons: dict[UUID, str] = {}
    requires_revision = False

    for worker_id in sorted(direct_consumers | planned_consumers, key=str):
        if worker_id in direct_consumers:
            if equivalent:
                reasons[worker_id] = "same_code_and_input"
            else:
                affected.add(worker_id)
                reasons[worker_id] = "consumes_changed_result"
            continue

        affected.add(worker_id)
        reasons[worker_id] = (
            "hard_edge_target_missing"
            if worker_id not in workers
            else "dependency_provenance_unknown"
        )
        if worker_id not in workers:
            requires_revision = True

    queue = sorted(affected, key=str)
    while queue:
        worker_id = queue.pop(0)
        for downstream in sorted(downstream_by_worker.get(worker_id, set()), key=str):
            if downstream in affected:
                continue
            affected.add(downstream)
            reasons[downstream] = (
                "hard_edge_target_missing"
                if downstream not in workers
                else "transitive_hard_dependency"
            )
            if downstream not in workers:
                requires_revision = True
            queue.append(downstream)
        queue.sort(key=str)

    reusable = workers - {changed_worker} - affected
    # A direct consumer whose replacement is equivalent is deliberately kept
    # out of affected and remains reusable with its original execution fact.
    return ReworkImpactV1(
        changed_result_id=changed_result_id,
        replacement_result_id=replacement.id,
        affected=tuple(sorted(affected, key=str)),
        reusable=tuple(sorted(reusable, key=str)),
        requires_revision=requires_revision,
        reason_by_worker=reasons,
    )


def _same_code_and_input(
    original: CodeResultSnapshotV1, replacement: CodeResultSnapshotV1
) -> bool:
    return (
        original.workspace_id == replacement.workspace_id
        and original.root_task_id == replacement.root_task_id
        and original.worker_item_id == replacement.worker_item_id
        and original.repository_ref == replacement.repository_ref
        and original.input_commit == replacement.input_commit
        and original.input_tree == replacement.input_tree
        and original.result_tree == replacement.result_tree
        and original.consumed_result_ids == replacement.consumed_result_ids
    )


__all__ = ["HardResultEdge", "ResultProvenance", "compute_rework_impact"]
