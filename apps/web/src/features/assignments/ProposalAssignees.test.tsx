import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { profileAt } from "@/features/agents/testFixtures";
import { assignmentTargetKey } from "./api";
import type { AssigneeValue, ProposalAssignmentTarget } from "./types";
import { ProposalAssignees } from "./ProposalAssignees";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "principal-current";
const PROFILE_ID = "30000000-0000-0000-0000-000000000001";
const REF = { workspace_id: WORKSPACE_ID, profile_id: PROFILE_ID, revision: 1 };

const targets: ProposalAssignmentTarget[] = [
  { kind: "subtask", key: "prepare", label: "Prepare", subtaskKey: null, strategy: "create", existingId: null },
  { kind: "subtask", key: "verify", label: "Verify", subtaskKey: null, strategy: "create", existingId: null },
  { kind: "worker_item", key: "inspect", label: "Inspect", subtaskKey: "prepare", strategy: "create", existingId: null },
  { kind: "worker_item", key: "implement", label: "Implement", subtaskKey: "prepare", strategy: "create", existingId: null },
  { kind: "worker_item", key: "check", label: "Check", subtaskKey: "verify", strategy: "create", existingId: null },
];
const profile: AssigneeValue = { kind: "profile", id: PROFILE_ID, profileRef: REF };

function renderRows(onChange = vi.fn()) {
  const choices = new Map(targets.map((target) => [assignmentTargetKey(target.kind, target.key), profile]));
  return {
    onChange,
    ...render(
      <ProposalAssignees
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        targets={targets}
        profiles={[profileAt(1)]}
        choices={choices}
        reusedBindings={new Map()}
        onChange={onChange}
      />,
    ),
  };
}

describe("initial proposal assignee rows", () => {
  it("shows all five independently selected Profile R1 owners grouped beneath their actual subtasks", () => {
    renderRows();

    expect(screen.getByRole("group", { name: "Prepare" })).toBeVisible();
    expect(screen.getByRole("group", { name: "Verify" })).toBeVisible();
    for (const label of ["Prepare owner", "Verify owner", "Inspect owner", "Implement owner", "Check owner"]) {
      expect(screen.getByLabelText(label)).toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 1]));
    }
    expect(screen.getAllByRole("option", { name: /Reusable reviewer · R1/ })).toHaveLength(5);
  });

  it("changes only the selected Subtask and does not cascade to its WorkerItems or write", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderRows(onChange);

    await user.selectOptions(screen.getByLabelText("Prepare owner"), "human");

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(
      assignmentTargetKey("subtask", "prepare"),
      { kind: "human", id: PRINCIPAL_ID, profileRef: null },
    );
    expect(screen.getByLabelText("Inspect owner")).toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 1]));
    expect(screen.getByLabelText("Implement owner")).toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 1]));
  });

  it("renders saved reuse pins read-only without an owner picker", () => {
    render(
      <ProposalAssignees
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        targets={[{ ...targets[0]!, strategy: "reuse", existingId: "a0000000-0000-0000-0000-000000000001" }]}
        profiles={[profileAt(1)]}
        choices={new Map()}
        reusedBindings={new Map([[assignmentTargetKey("subtask", "a0000000-0000-0000-0000-000000000001"), {
          workspace_id: WORKSPACE_ID,
          target_type: "subtask",
          target_id: "a0000000-0000-0000-0000-000000000001",
          assignee: { kind: "profile", id: PROFILE_ID },
          profile_ref: REF,
          version: 4,
          created_at: "2026-10-06T12:00:00Z",
          changed_at: "2026-10-06T12:00:00Z",
          runnable: true,
          not_runnable_reasons: [],
        }]])}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText(/Reusable reviewer.*R1/)).toBeVisible();
    expect(screen.getByText(/Binding V4/)).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Prepare owner" })).not.toBeInTheDocument();
  });

  it("shows the actual saved human identity and binding version read-only", () => {
    const humanId = "human-saved-owner";
    const targetId = "a0000000-0000-0000-0000-000000000002";
    render(
      <ProposalAssignees
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        targets={[{ ...targets[0]!, strategy: "reuse", existingId: targetId }]}
        profiles={[]}
        choices={new Map()}
        reusedBindings={new Map([[assignmentTargetKey("subtask", targetId), {
          workspace_id: WORKSPACE_ID,
          target_type: "subtask",
          target_id: targetId,
          assignee: { kind: "human", id: humanId },
          profile_ref: null,
          version: 8,
          created_at: "2026-10-06T12:00:00Z",
          changed_at: "2026-10-06T12:00:00Z",
          runnable: true,
          not_runnable_reasons: [],
        }]])}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText(`${humanId} · Binding V8`)).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Prepare owner" })).not.toBeInTheDocument();
  });
});
