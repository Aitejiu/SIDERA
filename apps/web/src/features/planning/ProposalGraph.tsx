import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";

import { layoutProposalGraph, type ProposalGraphLayout } from "./proposalGraphLayout";
import type { ProposalViewModel } from "./proposalTypes";

type ProposalFlowNodeData = {
  title: string;
  kind: "subtask" | "worker_item";
};
type ProposalFlowNode = Node<ProposalFlowNodeData, "proposal">;

const nodeTypes = { proposal: ProposalNode };

export function ProposalGraph({ model }: { model: ProposalViewModel }) {
  const [mode, setMode] = useState<"subtasks" | "workers">("subtasks");
  const [selectedSubtaskKey, setSelectedSubtaskKey] = useState(model.subtasks[0]?.key ?? "");
  const graph = useMemo(() => {
    if (mode === "subtasks") {
      return {
        nodes: model.subtasks.map((subtask) => ({ id: subtask.key, title: subtask.title, kind: "subtask" as const })),
        edges: model.subtaskDependencies.map((edge) => ({
          source: edge.upstreamSubtaskKey,
          target: edge.downstreamSubtaskKey,
          kind: edge.kind,
        })),
      };
    }
    const workerItems = model.workerItems.filter((worker) => worker.subtaskKey === selectedSubtaskKey);
    const workerKeys = new Set(workerItems.map((worker) => worker.key));
    return {
      nodes: workerItems.map((worker) => ({ id: worker.key, title: worker.title, kind: "worker_item" as const })),
      edges: model.workerItemDependencies
        .filter((edge) => workerKeys.has(edge.upstreamWorkerItemKey) && workerKeys.has(edge.downstreamWorkerItemKey))
        .map((edge) => ({ source: edge.upstreamWorkerItemKey, target: edge.downstreamWorkerItemKey, kind: edge.kind })),
    };
  }, [mode, model, selectedSubtaskKey]);
  const graphKey = `${mode}|${selectedSubtaskKey}|${graph.nodes.map((node) => node.id).join(",")}|${graph.edges.map((edge) => `${edge.source}:${edge.target}:${edge.kind}`).join(",")}`;
  const [layoutState, setLayoutState] = useState<{ graphKey: string; layout: ProposalGraphLayout | null; error: string | null } | null>(null);

  useEffect(() => {
    let active = true;
    void layoutProposalGraph(graph.nodes, graph.edges)
      .then((layout) => {
        if (active) setLayoutState({ graphKey, layout, error: null });
      })
      .catch(() => {
        if (active) setLayoutState({ graphKey, layout: null, error: "DAG 布局暂不可用，已保留完整依赖清单。" });
      });
    return () => {
      active = false;
    };
  }, [graph.edges, graph.nodes, graphKey]);

  const layout = layoutState?.graphKey === graphKey ? layoutState.layout : null;
  return (
    <section className="planning-proposal__graph" aria-labelledby="proposal-graph-title">
      <div className="planning-section-heading">
        <div>
          <span className="eyebrow">DEPENDENCY VIEW</span>
          <h2 id="proposal-graph-title">依赖 DAG</h2>
        </div>
        <div className="planning-proposal__graph-switch" role="group" aria-label="DAG 范围">
          <button type="button" className={mode === "subtasks" ? "is-active" : ""} onClick={() => setMode("subtasks")}>子任务</button>
          <button type="button" className={mode === "workers" ? "is-active" : ""} onClick={() => setMode("workers")}>WorkerItem</button>
        </div>
      </div>
      {mode === "workers" ? (
        <div className="planning-proposal__graph-subtask-picker" role="group" aria-label="选择子任务">
          {model.subtasks.map((subtask) => (
            <button key={subtask.key} type="button" className={selectedSubtaskKey === subtask.key ? "is-active" : ""} onClick={() => setSelectedSubtaskKey(subtask.key)}>
              {subtask.title}
            </button>
          ))}
        </div>
      ) : null}
      <div className="planning-proposal__graph-canvas" data-testid="proposal-graph-canvas">
        {layout !== null ? <Flow layout={layout} onSelectSubtask={setSelectedSubtaskKey} /> : <div className="planning-proposal__graph-loading" role="status">正在计算稳定布局</div>}
      </div>
      <div className="planning-proposal__edge-summary" role="group" aria-label="方案依赖清单">
        {graph.edges.map((edge) => (
          <span key={`${edge.source}:${edge.target}:${edge.kind}`} className={`edge-summary edge-summary--${edge.kind}`}>
            {titleFor(graph.nodes, edge.source)} → {titleFor(graph.nodes, edge.target)}
          </span>
        ))}
        {graph.edges.length === 0 ? <span className="planning-proposal__empty">当前范围没有显式依赖，节点可以并行准备。</span> : null}
      </div>
    </section>
  );
}

function Flow({ layout, onSelectSubtask }: { layout: ProposalGraphLayout; onSelectSubtask: (key: string) => void }) {
  const nodes: ProposalFlowNode[] = layout.nodes.map((node) => ({
    id: node.id,
    position: { x: node.x, y: node.y },
      data: { title: node.title, kind: node.kind },
    draggable: false,
    selectable: true,
      type: "proposal",
  }));
  const edges: Edge[] = layout.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: "smoothstep",
    style: {
      stroke: edge.kind === "informational" ? "#929aa4" : "#2166d1",
      strokeDasharray: edge.kind === "informational" ? "6 4" : undefined,
      strokeWidth: edge.kind === "hard" ? 2.5 : 1.5,
    },
  }));
  const onNodeClick: NodeMouseHandler<ProposalFlowNode> = (_event, node) => {
    if (node.data.kind === "subtask") onSelectSubtask(node.id);
  };
  return typeof ResizeObserver === "function" ? (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodeClick={onNodeClick}
      nodesDraggable={false}
      nodesConnectable={false}
      fitView
      fitViewOptions={{ padding: 0.18, minZoom: 0.35, maxZoom: 1.1 }}
      proOptions={{ hideAttribution: true }}
    >
      <Background color="#dfe3e7" gap={24} size={1} />
      <Controls showInteractive={false} />
    </ReactFlow>
  ) : <FallbackLayout layout={layout} onSelectSubtask={onSelectSubtask} />;
}

function ProposalNode({ data }: NodeProps<ProposalFlowNode>) {
  return (
    <>
      <Handle type="target" position={Position.Left} aria-hidden="true" />
      <button className="proposal-flow-node" type="button" aria-label={`查看${data.kind === "subtask" ? "子任务" : "WorkerItem"}：${data.title}`}>
        <span className="eyebrow">{data.kind === "subtask" ? "SUBTASK" : "WORKER ITEM"}</span>
        <strong>{data.title}</strong>
        <span>{data.kind === "subtask" ? "主任务依赖节点" : "WorkerItem 依赖节点"}</span>
      </button>
      <Handle type="source" position={Position.Right} aria-hidden="true" />
    </>
  );
}

function FallbackLayout({ layout, onSelectSubtask }: { layout: ProposalGraphLayout; onSelectSubtask: (key: string) => void }) {
  return (
    <div className="planning-proposal__graph-fallback" style={{ minHeight: `${Math.max(layout.height, 240)}px` }}>
      {layout.nodes.map((node) => (
        <button key={node.id} type="button" style={{ left: `${node.x}px`, top: `${node.y}px` }} onClick={() => onSelectSubtask(node.id)}>
          <strong>{node.title}</strong><span>{node.kind === "subtask" ? "子任务" : "WorkerItem"}</span>
        </button>
      ))}
    </div>
  );
}

function titleFor(nodes: readonly { id: string; title: string }[], id: string): string {
  return nodes.find((node) => node.id === id)?.title ?? id;
}
