import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk.bundled";

export const TASK_NODE_WIDTH = 280;
export const TASK_NODE_HEIGHT = 88;

export interface LayoutNodeInput {
  work_item_key: string;
}

export interface LayoutEdgeInput {
  upstream_work_item_key: string;
  downstream_work_item_key: string;
  kind: string;
  enforcement: string;
  scheduler_enforced: boolean;
  required_evidence_refs: readonly string[];
}

export interface LayoutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  data: LayoutNodeInput;
}

export interface LayoutEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  enforcement: string;
  scheduler_enforced: boolean;
  required_evidence_refs: readonly string[];
}

export interface TaskGraphLayout {
  width: number;
  height: number;
  nodes: LayoutNode[];
  edges: LayoutEdge[];
}

const layoutOptions = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.spacing.nodeNode": "36",
  "elk.layered.spacing.nodeNodeBetweenLayers": "84",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
};

export async function layoutTaskGraph(
  nodes: readonly LayoutNodeInput[],
  edges: readonly LayoutEdgeInput[],
): Promise<TaskGraphLayout> {
  const orderedNodes = [...nodes].sort((left, right) =>
    left.work_item_key.localeCompare(right.work_item_key),
  );
  const orderedEdges = [...edges].sort((left, right) =>
    edgeId(left).localeCompare(edgeId(right)),
  );
  const graph: ElkNode & { edges: ElkExtendedEdge[] } = {
    id: "task-graph",
    layoutOptions,
    children: orderedNodes.map((node) => ({
      id: node.work_item_key,
      width: TASK_NODE_WIDTH,
      height: TASK_NODE_HEIGHT,
    })),
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      sources: [edge.upstream_work_item_key],
      targets: [edge.downstream_work_item_key],
    })),
  };
  const layout = await new ELK().layout(graph);
  const positions = new Map(
    (layout.children ?? []).map((node) => [
      node.id,
      {
        x: node.x ?? 0,
        y: node.y ?? 0,
        width: node.width ?? TASK_NODE_WIDTH,
        height: node.height ?? TASK_NODE_HEIGHT,
      },
    ]),
  );
  const resultNodes = orderedNodes.map((node) => {
    const position = positions.get(node.work_item_key) ?? {
      x: 0,
      y: 0,
      width: TASK_NODE_WIDTH,
      height: TASK_NODE_HEIGHT,
    };
    return { id: node.work_item_key, ...position, data: node };
  });
  return {
    width: layout.width ?? 0,
    height: layout.height ?? 0,
    nodes: resultNodes,
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      source: edge.upstream_work_item_key,
      target: edge.downstream_work_item_key,
      kind: edge.kind,
      enforcement: edge.enforcement,
      scheduler_enforced: edge.scheduler_enforced,
      required_evidence_refs: edge.required_evidence_refs,
    })),
  };
}

function edgeId(edge: LayoutEdgeInput): string {
  return `${edge.upstream_work_item_key}:${edge.downstream_work_item_key}:${edge.kind}`;
}
