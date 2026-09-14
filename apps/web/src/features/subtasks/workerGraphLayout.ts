import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk.bundled";

import type { WorkerItemGraph, WorkerItemGraphEdge, WorkerItemSummary } from "./types";

export const WORKER_NODE_WIDTH = 300;
export const WORKER_NODE_HEIGHT = 156;

export interface WorkerLayoutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  data: WorkerItemSummary;
}

export interface WorkerLayoutEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
}

export interface WorkerGraphLayout {
  width: number;
  height: number;
  nodes: WorkerLayoutNode[];
  edges: WorkerLayoutEdge[];
}

const layoutOptions = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.spacing.nodeNode": "36",
  "elk.layered.spacing.nodeNodeBetweenLayers": "84",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
};

export async function layoutWorkerGraph(
  workers: readonly WorkerItemSummary[],
  graph: WorkerItemGraph,
): Promise<WorkerGraphLayout> {
  const workersById = new Map(workers.map((worker) => [worker.worker_item_id, worker]));
  const orderedNodes = [...graph.worker_item_ids]
    .map((workerItemId) => workersById.get(workerItemId))
    .filter((worker): worker is WorkerItemSummary => worker !== undefined)
    .sort((left, right) => left.worker_item_id.localeCompare(right.worker_item_id));
  const nodeIds = new Set(orderedNodes.map((worker) => worker.worker_item_id));
  const orderedEdges = graph.edges
    .filter((edge) => nodeIds.has(edge.upstream_worker_item_id) && nodeIds.has(edge.downstream_worker_item_id))
    .sort((left, right) => edgeId(left).localeCompare(edgeId(right)));
  const elkGraph: ElkNode & { edges: ElkExtendedEdge[] } = {
    id: "worker-graph",
    layoutOptions,
    children: orderedNodes.map((worker) => ({
      id: worker.worker_item_id,
      width: WORKER_NODE_WIDTH,
      height: WORKER_NODE_HEIGHT,
    })),
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      sources: [edge.upstream_worker_item_id],
      targets: [edge.downstream_worker_item_id],
    })),
  };
  const layout = await new ELK().layout(elkGraph);
  const positions = new Map(
    (layout.children ?? []).map((node) => [
      node.id,
      {
        x: node.x ?? 0,
        y: node.y ?? 0,
        width: node.width ?? WORKER_NODE_WIDTH,
        height: node.height ?? WORKER_NODE_HEIGHT,
      },
    ]),
  );
  const resultNodes = orderedNodes.map((worker) => {
    const position = positions.get(worker.worker_item_id) ?? {
      x: 0,
      y: 0,
      width: WORKER_NODE_WIDTH,
      height: WORKER_NODE_HEIGHT,
    };
    return { id: worker.worker_item_id, ...position, data: worker };
  });
  return {
    width: layout.width ?? 0,
    height: layout.height ?? 0,
    nodes: resultNodes,
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      source: edge.upstream_worker_item_id,
      target: edge.downstream_worker_item_id,
      kind: edge.kind,
    })),
  };
}

function edgeId(edge: WorkerItemGraphEdge): string {
  return `${edge.upstream_worker_item_id}:${edge.downstream_worker_item_id}:${edge.kind}`;
}
