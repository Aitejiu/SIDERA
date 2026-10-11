import { ArrowRightLeft, History, RefreshCw, Save, Search } from "lucide-react";

import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import { AssigneePicker } from "./AssigneePicker";
import type { AssigneeValue } from "./types";
import { useAssigneeAppointment } from "./useAssigneeAppointment";

interface AssigneeAppointmentProps {
  workspaceId: string;
  targetKind: "subtask" | "worker_item";
  targetId: string | null;
  workerWorkspace?: WorkerWorkspace;
  onRefresh: () => Promise<void>;
  onNativeHandoffRequest?: (target: AssigneeValue) => void;
  onNativeHandoffHistoryRequest?: () => void;
}

export function AssigneeAppointment({
  workspaceId,
  targetKind,
  targetId,
  workerWorkspace,
  onRefresh,
  onNativeHandoffRequest,
  onNativeHandoffHistoryRequest,
}: AssigneeAppointmentProps) {
  const appointment = useAssigneeAppointment({
    workspaceId,
    targetKind,
    targetId,
    ...(workerWorkspace === undefined ? {} : { workerWorkspace }),
    onRefresh,
  });

  if (appointment.mode === "legacy") return null;

  if (targetId === null || targetId.trim().length === 0) {
    return (
      <section className="worker-item-sidebar__section" aria-label="归属设置">
        <h3>归属</h3>
        <p role="status">没有可设置归属的实体子任务</p>
        {targetKind === "worker_item" && onNativeHandoffHistoryRequest !== undefined ? (
          <button className="quiet-button" type="button" onClick={onNativeHandoffHistoryRequest}>
            <History size={14} aria-hidden="true" />查看交接记录
          </button>
        ) : null}
      </section>
    );
  }

  const principalId = appointment.scope?.principal_id ?? appointment.session?.principal_id ?? "";
  const binding = appointment.binding;
  const originalStatus = appointment.status;
  const isUnresolved = originalStatus === "pending" || originalStatus === "unknown";
  const canQueryOriginal = isUnresolved
    || originalStatus === "applied" && appointment.original !== null && !appointment.saved;
  const isReviewableAgain = originalStatus === "rejected" || originalStatus === "applied"
    || originalStatus === "idle" && appointment.isStale;
  const currentOwner = binding === undefined
    ? "正在读取"
    : binding === null
      ? "未分配"
      : binding.assignee.kind === "human"
        ? binding.assignee.id === principalId ? "当前用户" : "其他用户"
        : binding.profile_ref === null
          ? `Profile ${binding.assignee.id}`
          : `${binding.profile_ref.profile_id} · R${binding.profile_ref.revision}`;
  const executionProfile = appointment.execution?.profile_ref;
  const runProfile = appointment.nativeContext?.run?.profile_ref;
  const choiceDiffersFromBinding = appointment.choice !== null && (
    binding === null || binding === undefined
    || binding.assignee.kind !== appointment.choice.kind
    || binding.assignee.id !== appointment.choice.id
    || appointment.choice.kind === "profile" && (
      binding.profile_ref === null
      || binding.profile_ref.workspace_id !== appointment.choice.profileRef.workspace_id
      || binding.profile_ref.profile_id !== appointment.choice.profileRef.profile_id
      || binding.profile_ref.revision !== appointment.choice.profileRef.revision
    )
  );
  const canStartNativeHandoff = targetKind === "worker_item"
    && onNativeHandoffRequest !== undefined
    && appointment.currentReviewResult?.ok === true
    && appointment.currentReviewResult.action === "handoff_required"
    && appointment.canEditChoice
    && !appointment.isStale
    && !appointment.busy
    && appointment.status === "idle"
    && appointment.original === null
    && appointment.storageError === null
    && !isUnresolved
    && choiceDiffersFromBinding;

  return (
    <section className="worker-item-sidebar__section" aria-label="归属设置">
      <h3>归属</h3>
      <dl className="worker-item-sidebar__facts">
        <div>
          <dt>当前归属</dt>
          <dd>{currentOwner}</dd>
        </div>
        {targetKind === "worker_item" ? (
          <>
            <div>
              <dt>执行 Profile</dt>
              <dd>{executionProfile === null || executionProfile === undefined
                ? "无已保存执行"
                : `${executionProfile.profile_id} · R${executionProfile.revision}`}</dd>
            </div>
            <div>
              <dt>Run Profile</dt>
              <dd>{runProfile === null || runProfile === undefined
                ? "无已保存 Run"
                : `${runProfile.profile_id} · R${runProfile.revision}`}</dd>
            </div>
            <div>
              <dt>执行归属版本</dt>
              <dd>{appointment.execution?.binding_version ?? "无"}</dd>
            </div>
          </>
        ) : null}
      </dl>

      {!appointment.sessionAuthorized ? (
        <LocalSessionButton workspaceId={workspaceId} onAuthorized={appointment.onSessionAuthorized} />
      ) : null}

      {principalId.length > 0 ? (
        <AssigneePicker
          workspaceId={workspaceId}
          principalId={principalId}
          label="设置归属"
          profiles={appointment.profiles}
          value={appointment.choice}
          onChange={appointment.onChoiceChange}
          disabled={!appointment.canEditChoice}
        />
      ) : null}

      {appointment.action === "handoff_required" ? (
        <p role="status">执行仍在运行，归属修改暂不可用</p>
      ) : null}
      {canStartNativeHandoff ? (
        <button
          className="quiet-button"
          type="button"
          onClick={() => {
            const target = appointment.choice;
            if (target !== null) {
              onNativeHandoffRequest(target.kind === "profile"
                ? { ...target, profileRef: { ...target.profileRef } }
                : { ...target });
            }
          }}
        >
          <ArrowRightLeft size={14} aria-hidden="true" />开始原生交接
        </button>
      ) : null}
      {targetKind === "worker_item" && onNativeHandoffHistoryRequest !== undefined ? (
        <button className="quiet-button" type="button" onClick={onNativeHandoffHistoryRequest}>
          <History size={14} aria-hidden="true" />查看交接记录
        </button>
      ) : null}
      {appointment.saved ? <p role="status">归属已确认</p> : null}
      {appointment.error !== null ? <p role="alert">{appointment.error}</p> : null}
      {appointment.disabledReason !== null && appointment.error === null
        && appointment.action !== "handoff_required" ? <p role="status">{appointment.disabledReason}</p> : null}

      {canQueryOriginal ? (
        <button
          className="quiet-button"
          type="button"
          disabled={!appointment.canLookupOriginal || appointment.busy}
          onClick={() => void appointment.lookupOriginal()}
        >
          <Search size={14} aria-hidden="true" />查询原始操作
        </button>
      ) : null}
      {isReviewableAgain ? (
        <button
          className="quiet-button"
          type="button"
          disabled={appointment.busy || appointment.scope === null}
          onClick={() => void appointment.revisit()}
        >
          <RefreshCw size={14} aria-hidden="true" />重新读取并复核
        </button>
      ) : null}
      {appointment.storageError !== null ? (
        <button className="quiet-button" type="button" onClick={appointment.retryDraftStorage}>
          重试读取本地记录
        </button>
      ) : null}
      <button
        className="quiet-button quiet-button--primary"
        type="button"
        disabled={!appointment.canSave || appointment.busy}
        title={appointment.disabledReason ?? undefined}
        onClick={() => void appointment.save()}
      >
        <Save size={14} aria-hidden="true" />{appointment.busy ? "正在处理" : "保存归属"}
      </button>
    </section>
  );
}
