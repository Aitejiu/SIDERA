import { ChevronDown, ChevronRight, GitBranch } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  layoutProposalGraph,
  PROPOSAL_NODE_HEIGHT,
  PROPOSAL_NODE_WIDTH,
  type ProposalGraphLayout,
} from "./proposalGraphLayout";
import type { RevisionDiff } from "./types";

type DependencyChange = RevisionDiff["subtask_dependency_changes"][number] | RevisionDiff["worker_dependency_changes"][number];
type DependencyEntry = DependencyChange & { scope: "subtask" | "worker_item" };

export function RevisionDagDiff({ diff }: { diff: RevisionDiff }) {
  const changes = useMemo<DependencyEntry[]>(() => [
    ...diff.subtask_dependency_changes.map((change) => ({ ...change, scope: "subtask" as const })),
    ...diff.worker_dependency_changes.map((change) => ({ ...change, scope: "worker_item" as const })),
  ], [diff.subtask_dependency_changes, diff.worker_dependency_changes]);
  const [open, setOpen] = useState(false);
  const graph = useMemo(() => buildGraph(changes), [changes]);
  const graphKey = `${graph.nodes.map((node) => node.id).join(",")}|${graph.edges.map((edge) => `${edge.source}:${edge.target}:${edge.kind}`).join(",")}`;
  const [layoutState, setLayoutState] = useState<{ graphKey: string; layout: ProposalGraphLayout | null; error: string | null }>({
    graphKey: "",
    layout: null,
    error: null,
  });

  useEffect(() => {
    if (!open || changes.length === 0) return undefined;
    let active = true;
    void layoutProposalGraph(graph.nodes, graph.edges)
      .then((layout) => {
        if (active) setLayoutState({ graphKey, layout, error: null });
      })
      .catch(() => {
        if (active) setLayoutState({ graphKey, layout: null, error: "DAG 布局暂不可用，已保留依赖清单。" });
      });
    return () => {
      active = false;
    };
  }, [changes.length, graph.edges, graph.nodes, graphKey, open]);

  if (changes.length === 0) {
    return (
      <section className="revision-review__dag" aria-labelledby="revision-dag-title">
        <div className="revision-review__section-heading">
          <div>
            <span className="eyebrow">OPTIONAL DEPENDENCY VIEW</span>
            <h2 id="revision-dag-title">依赖变化</h2>
          </div>
        </div>
        <p className="revision-review__empty">这次没有依赖变化，不需要展开 DAG。</p>
      </section>
    );
  }

  const layout = layoutState.graphKey === graphKey ? layoutState.layout : null;
  return (
    <section className="revision-review__dag" aria-labelledby="revision-dag-title">
      <div className="revision-review__section-heading">
        <div>
          <span className="eyebrow">OPTIONAL DEPENDENCY VIEW</span>
          <h2 id="revision-dag-title">依赖变化</h2>
        </div>
        <button
          className="revision-review__dag-toggle"
          type="button"
          aria-expanded={open}
          aria-controls="revision-dag-change-panel"
          onClick={() => setOpen((current) => !current)}
        >
          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          {open ? "收起依赖变化" : "查看依赖变化"}
        </button>
      </div>
      <p className="revision-review__dag-hint">默认只保留发生变化的依赖边，不展开完整执行图。</p>
      {open ? (
        <div id="revision-dag-change-panel" className="revision-review__dag-panel" role="region" aria-label="Revision 依赖变化">
          {layoutState.error ? <p className="revision-review__dag-error" role="alert">{layoutState.error}</p> : null}
          {layout === null && layoutState.error === null ? <div className="revision-review__dag-loading" role="status">正在计算稳定布局</div> : null}
          {layout !== null ? <DagCanvas layout={layout} removedNodeIds={graph.removedNodeIds} /> : null}
          <div className="revision-review__edge-list" role="group" aria-label="已变更依赖边">
            {changes.map((change, index) => (
              <span key={`${change.scope}:${change.upstream_key}:${change.downstream_key}:${change.change_type}:${index}`}>
                <GitBranch size={12} aria-hidden="true" />
                {change.upstream_path} → {change.downstream_path}
                <em>{dependencyChangeLabel(change.change_type)}</em>
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function DagCanvas({ layout, removedNodeIds }: { layout: ProposalGraphLayout; removedNodeIds: Set<string> }) {
  const width = Math.max(layout.width, PROPOSAL_NODE_WIDTH) + 32;
  const height = Math.max(layout.height, PROPOSAL_NODE_HEIGHT) + 32;
  const nodesById = new Map(layout.nodes.map((node) => [node.id, node]));
  return (
    <div className="revision-review__dag-canvas" data-testid="revision-dag-change-canvas" role="img" aria-label="变更依赖 DAG" style={{ minHeight: `${height}px` }}>
      <div className="revision-review__dag-canvas-inner" style={{ width: `${width}px`, minHeight: `${height}px` }}>
        <svg className="revision-review__dag-edges" width={width} height={height} aria-hidden="true">
          <defs>
            <marker id="revision-dag-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
              <path d="M0,0 L7,3.5 L0,7 z" fill="currentColor" />
            </marker>
          </defs>
          {layout.edges.map((edge) => {
            const source = nodesById.get(edge.source);
            const target = nodesById.get(edge.target);
            if (source === undefined || target === undefined) return null;
            return (
              <line
                key={edge.id}
                x1={source.x + source.width + 16}
                y1={source.y + source.height / 2 + 16}
                x2={target.x + 16}
                y2={target.y + target.height / 2 + 16}
                className={`revision-review__dag-edge revision-review__dag-edge--${edge.kind}`}
                markerEnd="url(#revision-dag-arrow)"
              />
            );
          })}
        </svg>
        {layout.nodes.map((node) => (
          <div
            className={`revision-review__dag-node${removedNodeIds.has(node.id) ? " revision-review__dag-node--removed" : ""}`}
            key={node.id}
            style={{ left: `${node.x + 16}px`, top: `${node.y + 16}px`, width: `${PROPOSAL_NODE_WIDTH}px`, minHeight: `${PROPOSAL_NODE_HEIGHT}px` }}
          >
            <span className="eyebrow">{node.kind === "worker_item" ? "WORKERITEM" : "SUBTASK"}</span>
            <strong>{node.title}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

function buildGraph(changes: DependencyEntry[]) {
  const nodes = new Map<string, { id: string; title: string; kind: "subtask" | "worker_item" }>();
  const edges = changes.map((change) => {
    const prefix = change.scope;
    const source = `${prefix}:${change.upstream_key}`;
    const target = `${prefix}:${change.downstream_key}`;
    nodes.set(source, { id: source, title: change.upstream_path, kind: prefix });
    nodes.set(target, { id: target, title: change.downstream_path, kind: prefix });
    return { source, target, kind: change.change_type };
  });
  const removedNodeIds = new Set(
    changes
      .filter((change) => change.change_type === "removed")
      .flatMap((change) => [`${change.scope}:${change.upstream_key}`, `${change.scope}:${change.downstream_key}`]),
  );
  return { nodes: [...nodes.values()], edges, removedNodeIds };
}

function dependencyChangeLabel(changeType: string): string {
  switch (changeType) {
    case "added":
      return "新增依赖";
    case "removed":
      return "移除依赖";
    case "changed":
      return "调整依赖";
    default:
      return changeType;
  }
}
