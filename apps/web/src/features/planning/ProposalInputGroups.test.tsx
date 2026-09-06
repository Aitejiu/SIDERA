import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { inputGroupFixture } from "@/test/resultInputFixtures";

import { ProposalInputGroups } from "./ProposalInputGroups";

describe("ProposalInputGroups", () => {
  it("shows an exact threshold and ordered sources", () => {
    render(<ProposalInputGroups groups={[inputGroupFixture()]} />);

    expect(screen.getByRole("region", { name: "结果输入规则" })).toHaveTextContent("至少 2 / 3");
    expect(screen.getByText("Research reports")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByRole("region", { name: "结果输入规则" })).toHaveTextContent("技术评估 / 汇总");
    expect(screen.getByText("artifact_type:report")).toBeVisible();
    expect(screen.getByText("acceptance:research-complete")).toBeVisible();
  });

  it("keeps all mode explicit and exposes independent hard dependencies", () => {
    const group = inputGroupFixture();
    render(
      <ProposalInputGroups
        groups={[{
          ...group,
          mode: "all",
          minimumCount: null,
          requiredCount: group.candidates.length,
          hardDependencies: [{
            upstreamWorkerItemKey: "setup",
            upstreamWorkerItemTitle: "Setup worker",
            requiredArtifactRefs: ["artifact_type:config"],
            requiredEvidenceRefs: [],
          }],
        }]}
      />,
    );

    expect(screen.getByText("全部 3 / 3")).toBeVisible();
    expect(screen.getByText("Setup worker")).toBeVisible();
    expect(screen.getByText("artifact_type:config")).toBeVisible();
  });
});
