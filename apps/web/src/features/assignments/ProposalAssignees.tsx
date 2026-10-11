import type { ProfileView } from "@/features/agents/types";
import { AssigneePicker } from "./AssigneePicker";
import { assignmentTargetKey } from "./api";
import type { AgentBindingView, AssigneeValue, ProposalAssignmentTarget } from "./types";

export function ProposalAssignees({
  workspaceId,
  principalId,
  targets,
  profiles,
  choices,
  reusedBindings,
  onChange,
  disabled = false,
}: {
  workspaceId: string;
  principalId: string;
  targets: readonly ProposalAssignmentTarget[];
  profiles: readonly ProfileView[];
  choices: ReadonlyMap<string, AssigneeValue>;
  reusedBindings: ReadonlyMap<string, AgentBindingView>;
  onChange: (targetKey: string, value: AssigneeValue | null) => void;
  disabled?: boolean;
}) {
  const subtasks = targets.filter((target) => target.kind === "subtask");
  const workers = targets.filter((target) => target.kind === "worker_item");
  const renderedWorkerKeys = new Set<string>();

  return (
    <div className="proposal-assignees" aria-label="提案负责人">
      {subtasks.map((subtask) => {
        const children = workers.filter((worker) => worker.subtaskKey === subtask.key);
        for (const child of children) renderedWorkerKeys.add(child.key);
        return (
          <div className="proposal-assignees__group" role="group" aria-label={subtask.label} key={subtask.key}>
            <AssignmentRow
              target={subtask}
              workspaceId={workspaceId}
              principalId={principalId}
              profiles={profiles}
              choices={choices}
              reusedBindings={reusedBindings}
              onChange={onChange}
              disabled={disabled}
            />
            {children.map((target) => (
              <AssignmentRow
                key={target.key}
                target={target}
                workspaceId={workspaceId}
                principalId={principalId}
                profiles={profiles}
                choices={choices}
                reusedBindings={reusedBindings}
                onChange={onChange}
                disabled={disabled}
              />
            ))}
          </div>
        );
      })}
      {workers.filter((worker) => !renderedWorkerKeys.has(worker.key)).map((target) => (
        <AssignmentRow
          key={target.key}
          target={target}
          workspaceId={workspaceId}
          principalId={principalId}
          profiles={profiles}
          choices={choices}
          reusedBindings={reusedBindings}
          onChange={onChange}
          disabled={disabled}
        />
      ))}
    </div>
  );
}

function AssignmentRow({
  target,
  workspaceId,
  principalId,
  profiles,
  choices,
  reusedBindings,
  onChange,
  disabled,
}: {
  target: ProposalAssignmentTarget;
  workspaceId: string;
  principalId: string;
  profiles: readonly ProfileView[];
  choices: ReadonlyMap<string, AssigneeValue>;
  reusedBindings: ReadonlyMap<string, AgentBindingView>;
  onChange: (targetKey: string, value: AssigneeValue | null) => void;
  disabled: boolean;
}) {
  const targetKey = assignmentTargetKey(target.kind, target.key);
  if (target.strategy === "reuse" && target.existingId !== null) {
    const binding = reusedBindings.get(assignmentTargetKey(target.kind, target.existingId));
    return (
      <div className="worker-runtime-field" role="group" aria-label={`${target.label} owner`}>
        <span>{target.label}</span>
        <span>{reuseOwnerLabel(binding, profiles)}</span>
      </div>
    );
  }

  return (
    <AssigneePicker
      workspaceId={workspaceId}
      principalId={principalId}
      label={`${target.label} owner`}
      profiles={profiles}
      value={choices.get(targetKey) ?? null}
      onChange={(value) => onChange(targetKey, value)}
      disabled={disabled}
    />
  );
}

function reuseOwnerLabel(binding: AgentBindingView | undefined, profiles: readonly ProfileView[]): string {
  if (binding === undefined) return "已保存的负责人不可用";
  if (binding.assignee.kind === "human") return `${binding.assignee.id} · Binding V${binding.version}`;
  const pin = binding.profile_ref;
  if (pin === null) return "已保存的 Profile 不可用";
  const profile = profiles.find((item) => item.profile.ref.workspace_id === pin.workspace_id
    && item.profile.ref.profile_id === pin.profile_id
    && item.profile.ref.revision === pin.revision);
  const name = profile?.profile.draft.name.trim() || `Profile ${pin.profile_id}`;
  return `${name} · R${pin.revision} · Binding V${binding.version}`;
}
