import { CircleAlert, RefreshCw, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef } from "react";

import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { HandoffProgress } from "@/features/worker-items/HandoffProgress";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import { AssigneePicker } from "./AssigneePicker";
import type { AssigneeValue } from "./types";
import { useProfileHandoff } from "./useProfileHandoff";

type ProfileHandoffDialogProps = {
  workspace: WorkerWorkspace;
  taskTitle: string;
  subtaskTitle: string;
  initialTarget: AssigneeValue | null;
  onRefresh: () => Promise<void>;
  onClose: () => void;
};

export function ProfileHandoffDialog({
  workspace,
  taskTitle,
  subtaskTitle,
  initialTarget,
  onRefresh,
  onClose,
}: ProfileHandoffDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const handoff = useProfileHandoff({ workspace, initialTarget, onRefresh });
  const busyRef = useRef(false);
  useLayoutEffect(() => {
    busyRef.current = handoff.busy;
  }, [handoff.busy]);
  const prepared = handoff.prepared;
  const isProgress = handoff.transitionId !== null;
  const isReview = !isProgress && prepared !== null;
  const source = prepared?.source_review ?? handoff.sourceReview;
  const principalId = handoff.scope?.principal_id ?? handoff.session?.principal_id ?? "";

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }, []);

  useEffect(() => {
    dialogRef.current?.focus();
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) {
        event.preventDefault();
        onClose();
      }
    };
    dialog.addEventListener("keydown", onKeyDown);
    return () => dialog.removeEventListener("keydown", onKeyDown);
  }, [isProgress, isReview, onClose]);

  useEffect(() => () => previousFocusRef.current?.focus(), []);

  const title = isProgress ? "原生交接进度" : isReview ? "复核原生交接方案" : "原生 Profile 交接";

  return (
    <div className="handoff-dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !handoff.busy) onClose();
    }}>
      <section
        className={`handoff-dialog${isProgress ? " handoff-dialog--progress" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={isProgress ? undefined : "profile-handoff-dialog-title"}
        aria-label={isProgress ? "原生交接进度" : undefined}
        ref={dialogRef}
        tabIndex={-1}
      >
        {isProgress ? (
          <>
            <div className="handoff-dialog__notice" role="status" aria-label="原始命令结果">
              Prepare：{actionStatusLabel(handoff.actions.prepare.status)} · Confirm：{actionStatusLabel(handoff.actions.confirm.status)}
            </div>
            {!handoff.sessionAuthorized || handoff.error === "需要重新确认当前用户会话" ? (
              <LocalSessionButton workspaceId={workspace.workspace_id} onAuthorized={handoff.onSessionAuthorized} />
            ) : null}
            {handoff.error !== null ? (
              <div className="handoff-dialog__error" role="alert">
                <CircleAlert size={14} aria-hidden="true" />{handoff.error}
              </div>
            ) : null}
            <HandoffProgress
              workspaceId={workspace.workspace_id}
              transitionId={handoff.transitionId ?? ""}
              traceDescriptor={workspace.trace_descriptor}
              onRefresh={onRefresh}
              onClose={onClose}
            />
            {handoff.canStartNew ? (
              <div className="handoff-dialog__actions">
                <button
                  className="quiet-button"
                  type="button"
                  disabled={handoff.busy}
                  onClick={() => void handoff.startNew()}
                >
                  开始新的交接
                </button>
              </div>
            ) : null}
          </>
        ) : (
          <>
            <div className="handoff-dialog__heading">
              <div>
                <span className="eyebrow">NATIVE PROFILE HANDOFF</span>
                <h2 id="profile-handoff-dialog-title">{title}</h2>
              </div>
              <button
                className="icon-button"
                type="button"
                aria-label="关闭原生交接"
                title="关闭原生交接"
                disabled={handoff.busy}
                onClick={onClose}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>

            <div className="handoff-dialog__scope">
              <span className="eyebrow">WORKER ITEM</span>
              <strong>{taskTitle} / {subtaskTitle} / {workspace.worker_item.title}</strong>
              <span>{workspace.worker_item.responsibility}</span>
            </div>

            {source ? (
              <dl className="handoff-dialog__facts handoff-dialog__facts--grid" aria-label="固定来源事实">
                <Fact label="来源 Profile" value={`${source.source_profile_ref.profile_id} · R${source.source_profile_ref.revision}`} />
                <Fact label="来源 Execution" value={source.execution_id} />
                <Fact label="来源 Run" value={source.run_id} />
                <Fact label="来源 Session" value={`${source.session_id} · V${source.session_version}`} />
                <Fact label="来源 Binding" value={`V${source.binding_version}`} />
                <Fact label="WorkerItem / Context" value={`V${source.worker_item_version} / V${source.context_version}`} />
              </dl>
            ) : (
              <div className="handoff-dialog__notice" role="status">正在读取并复核当前 native 来源事实</div>
            )}

            {!handoff.sessionAuthorized ? (
              <LocalSessionButton workspaceId={workspace.workspace_id} onAuthorized={handoff.onSessionAuthorized} />
            ) : null}

            {!isReview ? (
              <div className="handoff-dialog__body">
                <div className="handoff-dialog__section-heading">
                  <div>
                    <span className="eyebrow">TARGET OWNER</span>
                    <h3>选择目标归属</h3>
                  </div>
                  <button
                    className="quiet-button"
                    type="button"
                    disabled={handoff.busy || handoff.scope === null}
                    onClick={() => void handoff.revisit()}
                  >
                    <RefreshCw size={14} aria-hidden="true" />重新读取事实
                  </button>
                </div>
                {principalId ? (
                  <AssigneePicker
                    workspaceId={workspace.workspace_id}
                    principalId={principalId}
                    label="交接目标"
                    profiles={handoff.profiles}
                    value={handoff.target}
                    onChange={handoff.setTarget}
                    disabled={!handoff.canEditTarget}
                  />
                ) : null}
                <div className="handoff-dialog__fields">
                  <label>
                    <span>交接原因</span>
                    <textarea
                      value={handoff.reason}
                      onChange={(event) => handoff.setReason(event.target.value)}
                      aria-label="交接原因"
                      rows={2}
                      disabled={!handoff.canEditTarget}
                    />
                  </label>
                  <label>
                    <span>交接指令</span>
                    <textarea
                      value={handoff.instruction}
                      onChange={(event) => handoff.setInstruction(event.target.value)}
                      aria-label="交接指令"
                      rows={3}
                      disabled={!handoff.canEditTarget}
                    />
                  </label>
                </div>
                {handoff.isStale ? (
                  <div className="handoff-dialog__stale" role="alert">
                    <CircleAlert size={15} aria-hidden="true" />来源事实已变化，原选择和文本已保留；请重新读取并复核。
                  </div>
                ) : null}
                {handoff.storageError !== null ? (
                  <div className="handoff-dialog__error" role="alert">
                    <CircleAlert size={14} aria-hidden="true" />本地交接记录不可用，提交已锁定。
                    <button className="quiet-button" type="button" onClick={handoff.retryDraftStorage}>重试本地记录</button>
                  </div>
                ) : null}
                {handoff.originalLookupNeeded ? (
                  <div className="handoff-dialog__notice" role="status">
                    原始操作状态：{actionStatusLabel(handoff.status)}。在查询确认前不会创建新请求。
                  </div>
                ) : null}
                {handoff.error !== null && handoff.storageError === null ? (
                  <div className="handoff-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{handoff.error}</div>
                ) : null}
              </div>
            ) : (
              <ProposalReview
                handoff={handoff}
                onBack={() => void handoff.revisit()}
              />
            )}

            <div className="handoff-dialog__actions">
              <button className="quiet-button" type="button" onClick={onClose} disabled={handoff.busy}>关闭</button>
              {handoff.originalLookupNeeded ? (
                <button
                  className="quiet-button"
                  type="button"
                  disabled={!handoff.canLookupOriginal}
                  onClick={() => void handoff.lookupOriginal()}
                >
                  查询原始操作
                </button>
              ) : null}
              {handoff.canStartNew ? (
                <button className="quiet-button" type="button" onClick={() => void handoff.startNew()}>
                  开始新的交接
                </button>
              ) : null}
              {isReview && handoff.canCancel ? (
                <button className="quiet-button" type="button" disabled={handoff.busy} onClick={() => void handoff.cancel()}>
                  取消未激活方案
                </button>
              ) : null}
              {isReview && handoff.canConfirm ? (
                <button className="primary-button" type="button" disabled={handoff.busy} onClick={() => void handoff.confirm()}>
                  确认交接
                </button>
              ) : !isReview ? (
                <button className="primary-button" type="button" disabled={!handoff.canPrepare} onClick={() => void handoff.prepare()}>
                  {handoff.busy ? "处理中" : "准备原生交接"}
                </button>
              ) : null}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function ProposalReview({
  handoff,
  onBack,
}: {
  handoff: ReturnType<typeof useProfileHandoff>;
  onBack: () => void;
}) {
  const proposal = handoff.proposal ?? handoff.prepared?.proposal;
  if (!proposal) {
    return (
      <div className="handoff-dialog__error" role="alert">
        <CircleAlert size={14} aria-hidden="true" />无法读取与原始 Prepare 对应的原生方案。
        <button className="quiet-button" type="button" onClick={onBack}>重新读取</button>
      </div>
    );
  }
  const stale = handoff.isStale || proposal.status !== "prepared" || proposal.stale_reason !== null;
  return (
    <div className="handoff-dialog__body">
      <div className="handoff-dialog__section-heading">
        <div>
          <span className="eyebrow">CANONICAL PROPOSAL · V{proposal.version}</span>
          <h3>复核固定方案</h3>
        </div>
        <span className="handoff-progress__status">{proposal.status}</span>
      </div>
      {stale ? (
        <div className="handoff-dialog__stale" role="alert">
          <CircleAlert size={15} aria-hidden="true" />方案或来源事实已变化；当前保存的目标、原因和指令保持不变。
        </div>
      ) : null}
      <div className="handoff-review-participants">
        <ReviewParticipant
          label="来源 Profile / Run"
          title={`${proposal.source_profile_ref.profile_id} · R${proposal.source_profile_ref.revision}`}
          detail={`${proposal.source_execution_id} · ${proposal.source_run_id}`}
        />
        <span aria-hidden="true">→</span>
        <ReviewParticipant
          label="目标归属"
          title={targetLabel(handoff.target, handoff.profiles, handoff.scope?.principal_id ?? "")}
          detail={proposal.target_profile_ref === null
            ? "Profile 方案未绑定目标 Runtime"
            : `${proposal.target_profile_ref.profile_id} · R${proposal.target_profile_ref.revision}`}
        />
      </div>
      <dl className="handoff-dialog__facts handoff-dialog__facts--grid">
        <Fact label="Proposal" value={`${proposal.id} · V${proposal.version}`} />
        <Fact label="来源 WorkerItem" value={`V${proposal.source_worker_item_version}`} />
        <Fact label="Context" value={`目标 Context V${proposal.context.target_context_version}`} />
        <Fact label="Plan" value={`Generation ${proposal.plan_base.generation}`} />
      </dl>
      <div className="handoff-review-request">
        <div><span>交接原因</span><strong>{proposal.handoff_reason}</strong></div>
        <div><span>交接指令</span><strong>{proposal.handoff_instruction}</strong></div>
      </div>
      <div className="handoff-dialog__notice" role="status">
        Prepare：{actionStatusLabel(handoff.actions.prepare.status)}。Confirm 仅在此不可变方案与当前来源事实完全一致时可用。
      </div>
      {handoff.error !== null ? <div className="handoff-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{handoff.error}</div> : null}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd><code>{value}</code></dd></div>;
}

function ReviewParticipant({ label, title, detail }: { label: string; title: string; detail: string }) {
  return (
    <div className="handoff-review-participant">
      <span className="eyebrow">{label}</span>
      <strong>{title}</strong>
      <small>{detail}</small>
    </div>
  );
}

function targetLabel(
  target: AssigneeValue | null,
  profiles: ReturnType<typeof useProfileHandoff>["profiles"],
  principalId: string,
): string {
  if (target === null) return "未选择";
  if (target.kind === "human") return target.id === principalId ? "当前用户" : "其他用户";
  const profile = profiles.find((item) => item.profile.ref.profile_id === target.profileRef.profile_id
    && item.profile.ref.revision === target.profileRef.revision);
  return `${profile?.profile.draft.name.trim() || `Profile ${target.profileRef.profile_id}`} · R${target.profileRef.revision}`;
}

function actionStatusLabel(status: string | null): string {
  const labels: Record<string, string> = {
    idle: "尚未提交",
    pending: "处理中",
    unknown: "结果待查询",
    applied: "命令已处理",
    rejected: "命令被拒绝",
  };
  return status === null ? "尚未提交" : labels[status] ?? status;
}
