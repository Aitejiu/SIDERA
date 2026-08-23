import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk.bundled";

export const PROPOSAL_NODE_WIDTH = 260;
export const PROPOSAL_NODE_HEIGHT = 104;

export interface ProposalGraphNodeInput {
  id: string;
  title: string;
  kind: "subtask" | "worker_item";
}

export interface ProposalGraphEdgeInput {
  source: string;
  target: string;
  kind: "hard" | "informational" | string;
}

export interface ProposalGraphLayoutNode extends ProposalGraphNodeInput {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ProposalGraphLayoutEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
}

export interface ProposalGraphLayout {
  width: number;
  height: number;
  nodes: ProposalGraphLayoutNode[];
  edges: ProposalGraphLayoutEdge[];
}

const layoutOptions = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.spacing.nodeNode": "36",
  "elk.layered.spacing.nodeNodeBetweenLayers": "84",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
};

export async function layoutProposalGraph(
  nodes: readonly ProposalGraphNodeInput[],
  edges: readonly ProposalGraphEdgeInput[],
): Promise<ProposalGraphLayout> {
  const orderedNodes = [...nodes].sort((left, right) => left.id.localeCompare(right.id));
  const nodeIds = new Set(orderedNodes.map((node) => node.id));
  const orderedEdges = [...edges]
    .filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .sort((left, right) => edgeId(left).localeCompare(edgeId(right)));

  const graph: ElkNode & { edges: ElkExtendedEdge[] } = {
    id: "proposal-graph",
    layoutOptions,
    children: orderedNodes.map((node) => ({
      id: node.id,
      width: PROPOSAL_NODE_WIDTH,
      height: PROPOSAL_NODE_HEIGHT,
    })),
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      sources: [edge.source],
      targets: [edge.target],
    })),
  };
  const layout = await new ELK().layout(graph);
  const positions = new Map(
    (layout.children ?? []).map((node) => [
      node.id,
      {
        x: node.x ?? 0,
        y: node.y ?? 0,
        width: node.width ?? PROPOSAL_NODE_WIDTH,
        height: node.height ?? PROPOSAL_NODE_HEIGHT,
      },
    ]),
  );

  return {
    width: layout.width ?? 0,
    height: layout.height ?? 0,
    nodes: orderedNodes.map((node) => ({
      ...node,
      ...(positions.get(node.id) ?? {
        x: 0,
        y: 0,
        width: PROPOSAL_NODE_WIDTH,
        height: PROPOSAL_NODE_HEIGHT,
      }),
    })),
    edges: orderedEdges.map((edge) => ({
      id: edgeId(edge),
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
    })),
  };
}

function edgeId(edge: ProposalGraphEdgeInput): string {
  return `${edge.source}:${edge.target}:${edge.kind}`;
}
