import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ResultPlanSummary } from "./ResultPlanSummary";
import { integrationSpecFixture } from "./testFixtures";

describe("ResultPlanSummary", () => {
  it("shows the concrete integration contract before confirmation", () => {
    render(
      <ResultPlanSummary
        proposalSchemaVersion={6}
        integrationSpec={integrationSpecFixture()}
        revisionChanges={["integration_spec.checks"]}
      />,
    );

    expect(screen.getByText("集成出口")).toBeVisible();
    expect(screen.getByText("integration-worker")).toBeVisible();
    expect(screen.getByText("单元测试通过")).toBeVisible();
    expect(screen.getByText("uv run pytest")).toBeVisible();
    expect(screen.getByText("最多 2 轮 / 900 秒")).toBeVisible();
    expect(screen.getByText("默认不自动应用")).toBeVisible();
    expect(screen.getByText("Revision 变动")).toBeVisible();
    expect(screen.getByText("integration_spec.checks")).toBeVisible();
  });

  it("does not invent integration details for an older proposal", () => {
    render(<ResultPlanSummary proposalSchemaVersion={2} integrationSpec={undefined} />);

    expect(screen.getByText("结果集成未启用")).toBeVisible();
    expect(screen.queryByText("默认不自动应用")).not.toBeInTheDocument();
  });
});
