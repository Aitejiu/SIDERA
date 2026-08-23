import { describe, expect, it } from "vitest";

import { layoutProposalGraph } from "./proposalGraphLayout";

const nodes = [
  { id: "execute", title: "执行", kind: "subtask" as const },
  { id: "prepare", title: "准备", kind: "subtask" as const },
  { id: "verify", title: "验证", kind: "subtask" as const },
  { id: "isolated", title: "独立", kind: "subtask" as const },
];

const edges = [
  { source: "prepare", target: "execute", kind: "hard" as const },
  { source: "prepare", target: "verify", kind: "informational" as const },
];

describe("layoutProposalGraph", () => {
  it("keeps proposal graph geometry deterministic across input order", async () => {
    const first = await layoutProposalGraph(nodes, edges);
    const shuffled = await layoutProposalGraph([...nodes].reverse(), [...edges].reverse());

    expect(first.nodes.map((node) => [node.id, node.x, node.y])).toEqual(
      shuffled.nodes.map((node) => [node.id, node.x, node.y]),
    );
    expect(first.nodes.find((node) => node.id === "prepare")?.x).toBeLessThan(
      first.nodes.find((node) => node.id === "execute")?.x ?? 0,
    );
    expect(first.nodes.some((node) => node.id === "isolated")).toBe(true);
  });

  it("keeps hard and informational dependency semantics in the result", async () => {
    const result = await layoutProposalGraph(nodes, edges);

    expect(result.edges.map((edge) => [edge.id, edge.kind])).toEqual([
      ["prepare:execute:hard", "hard"],
      ["prepare:verify:informational", "informational"],
    ]);
  });
});
