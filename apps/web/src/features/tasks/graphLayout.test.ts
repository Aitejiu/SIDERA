import { describe, expect, it } from "vitest";

import { layoutTaskGraph } from "./graphLayout";

const nodes = [
  { subtask_key: "integration", title: "Integrate", state: "planned" },
  { subtask_key: "build", title: "Build", state: "running" },
  { subtask_key: "design", title: "Design", state: "succeeded" },
  { subtask_key: "test", title: "Test", state: "planned" },
  { subtask_key: "isolated", title: "Isolated", state: "planned" },
];

const edges = [
  {
    upstream_subtask_key: "design",
    downstream_subtask_key: "integration",
    kind: "hard" as const,
  },
  {
    upstream_subtask_key: "build",
    downstream_subtask_key: "integration",
    kind: "informational" as const,
  },
  {
    upstream_subtask_key: "integration",
    downstream_subtask_key: "test",
    kind: "hard" as const,
  },
];

describe("layoutTaskGraph", () => {
  it("creates deterministic left-to-right ranks for parallel branches", async () => {
    const first = await layoutTaskGraph(nodes, edges);
    const shuffled = await layoutTaskGraph(
      [...nodes].reverse(),
      [...edges].reverse(),
    );

    expect(first.nodes.map((node) => [node.id, node.x, node.y])).toEqual(
      shuffled.nodes.map((node) => [node.id, node.x, node.y]),
    );
    expect(first.nodes.find((node) => node.id === "design")?.x).toBeLessThan(
      first.nodes.find((node) => node.id === "integration")?.x ?? 0,
    );
    expect(first.nodes.find((node) => node.id === "build")?.x).toBeLessThan(
      first.nodes.find((node) => node.id === "integration")?.x ?? 0,
    );

    const parallel = first.nodes.filter((node) => ["design", "build"].includes(node.id));
    expect(overlap(parallel[0], parallel[1])).toBe(false);
    expect(first.nodes.some((node) => node.id === "isolated")).toBe(true);
  });

  it("keeps dependency semantics separate from layout geometry", async () => {
    const result = await layoutTaskGraph(nodes, edges);

    expect(result.edges.map((edge) => [edge.id, edge.kind])).toEqual([
      ["build:integration:informational", "informational"],
      ["design:integration:hard", "hard"],
      ["integration:test:hard", "hard"],
    ]);
  });
});

function overlap(
  left: { x: number; y: number; width: number; height: number } | undefined,
  right: { x: number; y: number; width: number; height: number } | undefined,
): boolean {
  if (!left || !right) {
    return false;
  }
  return !(
    left.x + left.width <= right.x ||
    right.x + right.width <= left.x ||
    left.y + left.height <= right.y ||
    right.y + right.height <= left.y
  );
}
