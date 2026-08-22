import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useState } from "react";

import { layoutTaskGraph, type TaskGraphLayout } from "./graphLayout";
import { TaskNode, type TaskFlowNode, type TaskFlowNodeData } from "./TaskNode";
import type { TaskGraph as TaskGraphProjection } from "./types";

const nodeTypes = { task: TaskNode };

export function TaskGraph({
  graph,
  onOpen,
}: {
  graph: TaskGraphProjection;
  onOpen: (subtaskKey: string) => void;
}) {
  const graphKey = `${graph.nodes.map((node) => node.subtask_key).join(",")}|${graph.edges.map((edge) => `${edge.upstream_subtask_key}:${edge.downstream_subtask_key}:${edge.kind}`).join(",")}`;
  const [layoutState, setLayoutState] = useState<{
    graphKey: string;
    layout: TaskGraphLayout | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    let active = true;
    void layoutTaskGraph(graph.nodes, graph.edges)
      .then((nextLayout) => {
        if (active) {
          setLayoutState({ graphKey, layout: nextLayout, error: null });
        }
      })
      .catch(() => {
        if (active) {
          setLayoutState({
            graphKey,
            layout: null,
            error: "DAG 布局暂不可用，已保留完整任务表。",
          });
        }
      });
    return () => {
      active = false;
    };
  }, [graph.edges, graph.nodes, graphKey]);

  const layout = layoutState?.graphKey === graphKey ? layoutState.layout : null;
  const error = layoutState?.graphKey === graphKey ? layoutState.error : null;

  if (error !== null) {
    return (
      <div className="task-graph task-graph--error">
        <div className="graph-error" role="alert">{error}</div>
        <FallbackNodes graph={graph} onOpen={onOpen} />
      </div>
    );
  }

  if (layout === null) {
    return <div className="task-graph task-graph--loading" role="status">正在计算 DAG 布局</div>;
  }

  const flowNodes: TaskFlowNode[] = layout.nodes.map((node) => ({
    id: node.id,
    type: "task",
    position: { x: node.x, y: node.y },
    data: {
      ...(node.data as TaskFlowNodeData),
    },
    draggable: false,
    selectable: false,
  }));
  const flowEdges: Edge[] = layout.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: "smoothstep" as const,
    animated: false,
    data: edge as unknown as Record<string, unknown>,
    style: {
      stroke: edge.kind === "informational" ? "#929aa4" : "#2166d1",
      strokeDasharray: edge.kind === "informational" ? "6 4" : undefined,
      strokeWidth: edge.kind === "hard" ? 2.5 : 1.5,
    },
  }));
  const onNodeClick: NodeMouseHandler<TaskFlowNode> = (_event, node) => {
    onOpen(node.id);
  };

  return (
    <div className="task-graph" role="region" aria-label="任务 DAG">
      <div className="task-graph__canvas">
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
          <FallbackLayoutNodes layout={layout} onOpen={onOpen} />
        )}
      </div>
      <div className="task-graph__edge-summary" role="group" aria-label="依赖关系">
        {layout.edges.map((edge) => (
          <span key={edge.id} className={`edge-summary edge-summary--${edge.kind}`}>
            依赖：{edge.source} → {edge.target}
          </span>
        ))}
      </div>
    </div>
  );
}

function FallbackNodes({
  graph,
  onOpen,
}: {
  graph: TaskGraphProjection;
  onOpen: (subtaskKey: string) => void;
}) {
  return (
    <div className="task-graph__fallback">
      {graph.nodes.map((node) => (
        <button
          key={node.subtask_key}
          type="button"
          className="task-graph__fallback-node"
          onClick={() => onOpen(node.subtask_key)}
          aria-label={`打开子任务：${node.title}`}
        >
          {node.title}
        </button>
      ))}
    </div>
  );
}

function FallbackLayoutNodes({
  layout,
  onOpen,
}: {
  layout: TaskGraphLayout;
  onOpen: (subtaskKey: string) => void;
}) {
  return (
    <div className="task-graph__fallback-layout" style={{ minHeight: `${Math.max(layout.height, 240)}px` }}>
      {layout.nodes.map((node) => (
        <button
          key={node.id}
          type="button"
          className="task-graph__fallback-node"
          style={{ left: `${node.x}px`, top: `${node.y}px` }}
          onClick={() => onOpen(node.id)}
          aria-label={`打开子任务：${(node.data as { title?: string }).title ?? node.id}`}
        >
          {(node.data as { title?: string }).title ?? node.id}
        </button>
      ))}
    </div>
  );
}
