import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PROFILE, PROFILE_ID, profileAt, WORKSPACE_ID } from "@/features/agents/testFixtures";
import type { AssigneeValue } from "./types";
import { AssigneePicker } from "./AssigneePicker";

const PRINCIPAL_ID = "principal-current";
const PROFILE_R1: AssigneeValue = {
  kind: "profile",
  id: PROFILE_ID,
  profileRef: PROFILE.profile.ref,
};

describe("AssigneePicker", () => {
  it("shows an explicit empty choice and emits the current human without changing its controlled value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <AssigneePicker
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        label="Subtask Build owner"
        profiles={[PROFILE]}
        value={null}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Subtask Build owner" });

    expect(select).toHaveValue("");
    expect(screen.getByRole("option", { name: "未分配" })).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    await user.selectOptions(select, "human");
    expect(onChange).toHaveBeenLastCalledWith({
      kind: "human",
      id: PRINCIPAL_ID,
      profileRef: null,
    });

    rerender(
      <AssigneePicker
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        label="Subtask Build owner"
        profiles={[PROFILE]}
        value={null}
        onChange={onChange}
      />,
    );
    expect(select).toHaveValue("");
  });

  it("offers exact enabled Profile revisions and emits the selected pin", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <AssigneePicker
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        label="Worker item Inspect owner"
        profiles={[PROFILE, profileAt(2)]}
        value={null}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Worker item Inspect owner" });
    const revisionOne = screen.getByRole("option", { name: "Reusable reviewer · R1" });
    const revisionTwo = screen.getByRole("option", { name: "Reusable reviewer · R2" });

    expect(revisionOne).toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 1]));
    expect(revisionTwo).toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 2]));
    await user.selectOptions(select, revisionOne);
    expect(onChange).toHaveBeenLastCalledWith(PROFILE_R1);
  });

  it("keeps a selected unavailable revision visible and disabled without upgrading it", () => {
    const onChange = vi.fn();
    render(
      <AssigneePicker
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        label="Worker item Inspect owner"
        profiles={[profileAt(2)]}
        value={PROFILE_R1}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Worker item Inspect owner" });
    const savedPin = screen.getByRole("option", { name: "Reusable reviewer · R1（不可用）" });

    expect(savedPin).toBeDisabled();
    expect(select).toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 1]));
    expect(select).not.toHaveValue(JSON.stringify(["profile", PROFILE_ID, WORKSPACE_ID, 2]));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not offer disabled or foreign Profiles and still allows the same Profile in another picker", async () => {
    const user = userEvent.setup();
    const firstChange = vi.fn();
    const secondChange = vi.fn();
    const disabled = { ...PROFILE, status: { ...PROFILE.status, enabled: false } };
    const foreign = {
      ...PROFILE,
      profile: {
        ...PROFILE.profile,
        ref: { ...PROFILE.profile.ref, workspace_id: "foreign-workspace" },
      },
    };
    render(
      <>
        <AssigneePicker
          workspaceId={WORKSPACE_ID}
          principalId={PRINCIPAL_ID}
          label="Subtask Review owner"
          profiles={[disabled, foreign, PROFILE]}
          value={null}
          onChange={firstChange}
        />
        <AssigneePicker
          workspaceId={WORKSPACE_ID}
          principalId={PRINCIPAL_ID}
          label="Worker item Inspect owner"
          profiles={[PROFILE]}
          value={null}
          onChange={secondChange}
        />
      </>,
    );
    const first = screen.getByRole("combobox", { name: "Subtask Review owner" });
    const second = screen.getByRole("combobox", { name: "Worker item Inspect owner" });
    expect(screen.getAllByRole("option", { name: "Reusable reviewer · R1" })).toHaveLength(2);
    await user.selectOptions(first, screen.getAllByRole("option", { name: "Reusable reviewer · R1" })[0]!);
    await user.selectOptions(second, screen.getAllByRole("option", { name: "Reusable reviewer · R1" })[1]!);
    expect(firstChange).toHaveBeenLastCalledWith(PROFILE_R1);
    expect(secondChange).toHaveBeenLastCalledWith(PROFILE_R1);
  });

  it("returns to unassigned when the empty option is selected", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <AssigneePicker
        workspaceId={WORKSPACE_ID}
        principalId={PRINCIPAL_ID}
        label="Subtask Review owner"
        profiles={[PROFILE]}
        value={{ kind: "human", id: PRINCIPAL_ID, profileRef: null }}
        onChange={onChange}
      />,
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "Subtask Review owner" }), "");
    expect(onChange).toHaveBeenLastCalledWith(null);
  });
});
