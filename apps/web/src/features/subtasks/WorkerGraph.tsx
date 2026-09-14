import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useState } from "react";

import { executionStatusLabel, StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import { layoutWorkerGraph, type WorkerGraphLayout } from "./workerGraphLayout";
import type { SubtaskWorkspace, WorkerItemSummary } from "./types";

type WorkerFlowNodeData = WorkerItemSummary & {
  statusKind: StatusMarkKind;
  onOpen: (workerItemId: string) => void;
};
type WorkerFlowNode = Node<WorkerFlowNodeData, "worker">;

const nodeTypes = { worker: WorkerGraphNode };

export function WorkerGraph({
  workspace,
  onOpen,
}: {
  workspace: SubtaskWorkspace;
  onOpen: (workerItemId: string) => void;
}) {
  const graphKey = `${workspace.worker_graph.worker_item_ids.join(",")}|${workspace.worker_graph.edges.map((edge) => `${edge.upstream_worker_item_id}:${edge.downstream_worker_item_id}:${edge.kind}`).join(",")}`;
  const [layoutState, setLayoutState] = useState<{
    graphKey: string;
    layout: WorkerGraphLayout | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    let active = true;
    void layoutWorkerGraph(workspace.worker_items, workspace.worker_graph)
      .then((layout) => {
        if (active) {
          setLayoutState({ graphKey, layout, error: null });
        }
      })
      .catch(() => {
        if (active) {
          setLayoutState({
            graphKey,
            layout: null,
            error: "Worker Graph 布局暂不可用，已保留 Worker 看板。",
          });
        }
      });
    return () => {
      active = false;
    };
  }, [graphKey, workspace.worker_graph, workspace.worker_items]);

  const layout = layoutState?.graphKey === graphKey ? layoutState.layout : null;
  const error = layoutState?.graphKey === graphKey ? layoutState.error : null;

  if (error !== null) {
    return (
      <section className="worker-graph worker-graph--error" aria-label="Worker Graph">
        <div className="graph-error" role="alert">{error}</div>
        <WorkerGraphFallback workspace={workspace} onOpen={onOpen} />
      </section>
    );
  }

  if (layout === null) {
    return <div className="worker-graph worker-graph--loading" role="status">正在计算 Worker Graph 布局</div>;
  }

  const flowNodes: WorkerFlowNode[] = layout.nodes.map((node) => ({
    id: node.id,
    type: "worker",
    position: { x: node.x, y: node.y },
    data: {
      ...node.data,
      statusKind: workerStatusKind(node.data.status),
      onOpen,
    },
    draggable: false,
    selectable: false,
  }));
  const flowEdges: Edge[] = layout.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: "smoothstep" as const,
    label: edge.kind,
    animated: edge.kind === "dependency",
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--graph-line)" },
    style: edgeStyle(edge.kind),
    data: edge as unknown as Record<string, unknown>,
  }));
  const onNodeClick: NodeMouseHandler<WorkerFlowNode> = (_event, node) => {
    onOpen(node.id);
  };

  return (
    <section className="worker-graph" aria-label="Worker Graph">
      <div className="worker-graph__canvas" data-testid="worker-graph-canvas">
        {typeof ResizeObserver === "function" ? (
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={nodeTypes}
            onNodeClick={onNodeClick}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            fitView
            fitViewOptions={{ padding: 0.18, minZoom: 0.35, maxZoom: 1.1 }}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#dfe3e7" gap={24} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        ) : (
          <WorkerGraphFallback workspace={workspace} layout={layout} onOpen={onOpen} />
        )}
      </div>
      <div className="worker-graph__legend" aria-label="Worker Graph 图例">
        <span className="worker-graph__legend-item worker-graph__legend-item--dependency">dependency</span>
        <span className="worker-graph__legend-item worker-graph__legend-item--review">review</span>
        <span className="worker-graph__legend-item worker-graph__legend-item--context">context</span>
      </div>
      <div className="worker-graph__edge-summary" role="group" aria-label="Worker 关系">
        {layout.edges.map((edge) => (
          <span key={edge.id} className={`worker-edge-summary worker-edge-summary--${edge.kind}`}>
            {workerTitle(workspace, edge.source)} → {workerTitle(workspace, edge.target)} · {edge.kind}
          </span>
        ))}
      </div>
    </section>
  );
}

function WorkerGraphNode({ data }: NodeProps<WorkerFlowNode>) {
  return (
    <>
      <Handle type="target" position={Position.Left} aria-hidden="true" />
      <button
        className="worker-flow-node"
        type="button"
        aria-label={`打开 Worker：${data.title}`}
        onClick={() => data.onOpen?.(data.worker_item_id)}
      >
        <span className="worker-flow-node__topline">
          <span className="eyebrow">{data.role}</span>
          <StatusMark kind={data.statusKind} label={executionStatusLabel(data.status)} />
        </span>
        <strong>{data.title}</strong>
        <span className="worker-flow-node__step">{data.current_step ?? "尚未开始"}</span>
      </button>
      <Handle type="source" position={Position.Right} aria-hidden="true" />
    </>
  );
}

function WorkerGraphFallback({
  workspace,
  layout,
  onOpen,
}: {
  workspace: SubtaskWorkspace;
  layout?: WorkerGraphLayout;
  onOpen: (workerItemId: string) => void;
}) {
  if (layout === undefined) {
    return (
      <div className="worker-graph__fallback">
        {workspace.worker_items.map((worker) => (
          <button key={worker.worker_item_id} type="button" onClick={() => onOpen(worker.worker_item_id)} aria-label={`打开 Worker：${worker.title}`}>
            {worker.title}
          </button>
        ))}
      </div>
    );
  }
  return (
    <div className="worker-graph__fallback-layout" style={{ minHeight: `${Math.max(layout.height, 240)}px` }}>
      {layout.nodes.map((node) => (
        <button
          key={node.id}
          type="button"
          style={{ left: `${node.x}px`, top: `${node.y}px` }}
          onClick={() => onOpen(node.id)}
          aria-label={`打开 Worker：${node.data.title}`}
        >
          {node.data.title}
        </button>
      ))}
    </div>
  );
}

function workerTitle(workspace: SubtaskWorkspace, workerItemId: string): string {
  return workspace.worker_items.find((worker) => worker.worker_item_id === workerItemId)?.title ?? workerItemId.slice(0, 8);
}

function edgeStyle(kind: string) {
  if (kind === "review") {
    return { stroke: "var(--attention)", strokeDasharray: "7 4", strokeWidth: 1.7 };
  }
  if (kind === "context") {
    return { stroke: "var(--graph-line)", strokeDasharray: "3 4", strokeWidth: 1.3 };
  }
  return { stroke: "var(--graph-line)", strokeWidth: 1.7 };
}

function workerStatusKind(status: string): StatusMarkKind {
  if (status === "running" || status === "assigned" || status === "queued" || status === "retrying") {
    return "running";
  }
  if (status === "succeeded" || status === "completed") {
    return "completed";
  }
  if (status === "failed") {
    return "failed";
  }
  if (status === "blocked" || status === "waiting_user" || status === "waiting_dependency") {
    return "stale";
  }
  return "planned";
}
