import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk.bundled";

export const TASK_NODE_WIDTH = 280;
export const TASK_NODE_HEIGHT = 140;

export interface LayoutNodeInput {
  subtask_key: string;
}

export interface LayoutEdgeInput {
  upstream_subtask_key: string;
  downstream_subtask_key: string;
  kind: string;
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
    left.subtask_key.localeCompare(right.subtask_key),
  );
  const orderedEdges = [...edges].sort((left, right) =>
    edgeId(left).localeCompare(edgeId(right)),
  );
  const graph: ElkNode & { edges: ElkExtendedEdge[] } = {
    id: "task-graph",
    layoutOptions,
    children: orderedNodes.map((node) => ({
      id: node.subtask_key,
      width: TASK_NODE_WIDTH,
      height: TASK_NODE_HEIGHT,
    })),
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      sources: [edge.upstream_subtask_key],
      targets: [edge.downstream_subtask_key],
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
    const position = positions.get(node.subtask_key) ?? {
      x: 0,
      y: 0,
      width: TASK_NODE_WIDTH,
      height: TASK_NODE_HEIGHT,
    };
    return { id: node.subtask_key, ...position, data: node };
  });
  return {
    width: layout.width ?? 0,
    height: layout.height ?? 0,
    nodes: resultNodes,
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      source: edge.upstream_subtask_key,
      target: edge.downstream_subtask_key,
      kind: edge.kind,
    })),
  };
}

function edgeId(edge: LayoutEdgeInput): string {
  return `${edge.upstream_subtask_key}:${edge.downstream_subtask_key}:${edge.kind}`;
}
