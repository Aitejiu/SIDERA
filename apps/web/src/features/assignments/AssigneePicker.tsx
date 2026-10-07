import type { ProfileView } from "@/features/agents/types";
import type { AssigneeValue } from "./types";

interface AssigneePickerProps {
  workspaceId: string;
  principalId: string;
  label: string;
  profiles: readonly ProfileView[];
  value: AssigneeValue | null;
  onChange: (value: AssigneeValue | null) => void;
  disabled?: boolean;
}

export function AssigneePicker({
  workspaceId,
  principalId,
  label,
  profiles,
  value,
  onChange,
  disabled = false,
}: AssigneePickerProps) {
  const profileOptions = profiles
    .filter((profile) => profile.status.enabled
      && profile.status.profile_id === profile.profile.ref.profile_id
      && profile.profile.ref.workspace_id === workspaceId
      && Number.isInteger(profile.profile.ref.revision)
      && profile.profile.ref.revision > 0)
    .map((profile) => {
      const ref = profile.profile.ref;
      return {
        value: profileValue(ref.profile_id, ref.workspace_id, ref.revision),
        name: profile.profile.draft.name.trim() || `Profile ${ref.profile_id}`,
        assignee: {
          kind: "profile" as const,
          id: ref.profile_id,
          profileRef: { ...ref },
        },
      };
    });
  const selectable = new Map<string, AssigneeValue>([
    ["human", { kind: "human", id: principalId, profileRef: null }],
    ...profileOptions.map(({ value: optionValue, assignee }) => [optionValue, assignee] as const),
  ]);
  const selectedValue = valueToken(value, principalId);
  const hasSelectedOption = selectedValue === "" || selectable.has(selectedValue);

  function change(nextValue: string) {
    if (nextValue === "") {
      onChange(null);
      return;
    }
    const assignee = selectable.get(nextValue);
    if (assignee !== undefined) {
      onChange(assignee.kind === "profile"
        ? { ...assignee, profileRef: { ...assignee.profileRef } }
        : { ...assignee });
    }
  }

  return (
    <label className="worker-runtime-field">
      <span>{label}</span>
      <select
        aria-label={label}
        disabled={disabled}
        value={selectedValue}
        onChange={(event) => change(event.currentTarget.value)}
      >
        <option value="">未分配</option>
        <option value="human">当前用户</option>
        {profileOptions.map((profile) => (
          <option key={profile.value} value={profile.value}>
            {profile.name} · R{profile.assignee.profileRef.revision}
          </option>
        ))}
        {!hasSelectedOption && value !== null ? (
          <option value={selectedValue} disabled>
            {unavailableLabel(value, profiles, workspaceId)}
          </option>
        ) : null}
      </select>
    </label>
  );
}

function profileValue(profileId: string, workspaceId: string, revision: number): string {
  return JSON.stringify(["profile", profileId, workspaceId, revision]);
}

function valueToken(value: AssigneeValue | null, principalId: string): string {
  if (value === null) return "";
  if (value.kind === "human" && value.id === principalId && value.profileRef === null) return "human";
  if (value.kind === "profile") {
    return profileValue(value.profileRef.profile_id, value.profileRef.workspace_id, value.profileRef.revision);
  }
  return JSON.stringify(["unavailable", value.kind, value.id]);
}

function unavailableLabel(
  value: AssigneeValue,
  profiles: readonly ProfileView[],
  workspaceId: string,
): string {
  if (value.kind === "human") return "已保存的用户（不可用）";
  const name = profiles.find((profile) =>
    profile.profile.ref.workspace_id === workspaceId
    && profile.profile.ref.profile_id === value.id,
  )?.profile.draft.name.trim() || `Profile ${value.id}`;
  return `${name} · R${value.profileRef.revision}（不可用）`;
}
