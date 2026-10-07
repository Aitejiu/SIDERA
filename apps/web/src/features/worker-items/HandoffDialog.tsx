import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  RefreshCcw,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type RefObject } from "react";

import { ApiProblem } from "@/shared/api/client";

import {
  cancelWorkerHandoff,
  confirmWorkerHandoff,
  getHandoffProposal,
  handoffProposalQueryKey,
  prepareWorkerHandoff,
} from "./api";
import { HandoffProgress } from "./HandoffProgress";
import {
  isLegacyHandoffProposal,
  isNativeHandoffProposal,
  type HandoffCandidate,
  type HandoffCandidateList,
  type HandoffProposal,
  type NativeHandoffProposal,
  type WorkerWorkspace,
} from "./types";

type HandoffDialogStep = "select" | "review" | "progress";

export function HandoffDialog({
  workspace,
  taskTitle,
  subtaskTitle,
  candidates,
  candidatesPending,
  candidatesError,
  onRefreshCandidates,
  onRefresh,
  onClose,
}: {
  workspace: WorkerWorkspace;
  taskTitle: string;
  subtaskTitle: string;
  candidates: HandoffCandidateList | undefined;
  candidatesPending: boolean;
  candidatesError: unknown;
  onRefreshCandidates: () => Promise<unknown>;
  onRefresh: () => Promise<void>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [step, setStep] = useState<HandoffDialogStep>("select");
  const [selectedWorkerIdState, setSelectedWorkerId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [instruction, setInstruction] = useState("");
  const [proposalId, setProposalId] = useState<string | null>(null);
  const [transitionId, setTransitionId] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "submitting" | "cancelling">("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const statusRef = useRef(status);
  const proposalQuery = useQuery({
    queryKey: handoffProposalQueryKey(workspace.workspace_id, proposalId ?? ""),
    queryFn: ({ signal }) => getHandoffProposal(workspace.workspace_id, proposalId ?? "", signal),
    enabled: proposalId !== null,
  });
  const fetchedProposal = proposalQuery.data;
  const legacyProposal = isLegacyHandoffProposal(fetchedProposal) ? fetchedProposal : undefined;
  const nativeProposal = isNativeHandoffProposal(fetchedProposal) ? fetchedProposal : undefined;

  const eligibleCandidates = (candidates?.candidates ?? []).filter((candidate) => candidate.eligible);
  const selectedWorkerId = selectedWorkerIdState !== null
    && eligibleCandidates.some((candidate) => candidate.worker_id === selectedWorkerIdState)
    ? selectedWorkerIdState
    : candidates?.recommended_worker_id ?? eligibleCandidates[0]?.worker_id ?? null;
  const selectedCandidate = (candidates?.candidates ?? []).find(
    (candidate) => candidate.worker_id === selectedWorkerId,
  );

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }, []);

  useEffect(() => {
    dialogRef.current?.focus();
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && statusRef.current === "idle") {
        event.preventDefault();
        onClose();
      }
    };
    dialog.addEventListener("keydown", onKeyDown);
    return () => dialog.removeEventListener("keydown", onKeyDown);
  }, [onClose, step]);

  useEffect(() => () => {
    previousFocusRef.current?.focus();
  }, []);

  const prepare = async () => {
    if (!selectedCandidate || reason.trim() === "" || instruction.trim() === "") return;
    setStatus("submitting");
    setErrorMessage(null);
    try {
      const response = await prepareWorkerHandoff(
        workspace.workspace_id,
        workspace.worker_item.id,
        candidates?.source_worker_item_version ?? workspace.worker_item.version,
        {
          target_worker_id: selectedCandidate.worker_id,
          handoff_reason: reason.trim(),
          handoff_instruction: instruction.trim(),
          proposal_ttl_seconds: 900,
          input_plan: { mode: "safe_only", source: "current_worker_item" },
        },
      );
      setProposalId(response.aggregate_id);
      setStep("review");
    } catch (error) {
      setErrorMessage(describeError(error));
    } finally {
      setStatus("idle");
    }
  };

  const cancel = async (proposal: HandoffProposal) => {
    setStatus("cancelling");
    setErrorMessage(null);
    try {
      await cancelWorkerHandoff(workspace.workspace_id, proposal, "用户取消本次 Agent 切换方案");
      await onRefresh();
      onClose();
    } catch (error) {
      setStatus("idle");
      setErrorMessage(describeError(error));
    }
  };

  const confirm = async (proposal: HandoffProposal) => {
    if (!canConfirmProposal(proposal)) return;
    setStatus("submitting");
    setErrorMessage(null);
    try {
      const response = await confirmWorkerHandoff(
        workspace.workspace_id,
        proposal,
        workspace.worker_item.version,
      );
      setTransitionId(response.aggregate_id);
      setStep("progress");
      await onRefresh();
    } catch (error) {
      setErrorMessage(describeError(error));
    } finally {
      setStatus("idle");
    }
  };

  return (
    <div className="handoff-dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && status === "idle") onClose();
    }}>
      {step === "select" ? (
        <section
          className="handoff-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="handoff-dialog-title"
          ref={dialogRef}
          tabIndex={-1}
        >
          <DialogHeading title="调整执行 Agent" eyebrow="WORKER HANDOFF / SELECT" onClose={onClose} />
          <HandoffScope taskTitle={taskTitle} subtaskTitle={subtaskTitle} workspace={workspace} />
          <div className="handoff-dialog__body">
            <div className="handoff-dialog__section-heading">
              <div>
                <span className="eyebrow">ELIGIBLE TARGETS</span>
                <h3>选择目标 Agent</h3>
              </div>
              <button className="quiet-button" type="button" onClick={() => void onRefreshCandidates()} disabled={candidatesPending || status !== "idle"}>
                <RefreshCcw size={14} aria-hidden="true" />刷新候选
              </button>
            </div>
            {candidatesPending ? <div className="handoff-dialog__notice" role="status">正在读取可接管的 Agent</div> : null}
            {candidatesError ? (
              <div className="handoff-dialog__error" role="alert">
                <CircleAlert size={14} aria-hidden="true" />
                <span>{describeError(candidatesError)}</span>
                <button className="quiet-button" type="button" onClick={() => void onRefreshCandidates()}>重试</button>
              </div>
            ) : null}
            {!candidatesPending && candidates && eligibleCandidates.length === 0 ? (
              <div className="handoff-dialog__notice" role="status">当前没有通过后端资格检查的目标 Agent。</div>
            ) : null}
            <div className="handoff-dialog__candidates">
              {(candidates?.candidates ?? []).map((candidate) => (
                <CandidateCard
                  key={candidate.worker_id}
                  candidate={candidate}
                  selected={candidate.worker_id === selectedWorkerId}
                  onSelect={() => setSelectedWorkerId(candidate.worker_id)}
                />
              ))}
            </div>
            <div className="handoff-dialog__fields">
              <label>
                <span>切换原因</span>
                <textarea
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  aria-label="切换原因"
                  rows={2}
                  placeholder="说明为什么要把这个 WorkerItem 交给另一个 Agent"
                />
              </label>
              <label>
                <span>交接指令</span>
                <textarea
                  value={instruction}
                  onChange={(event) => setInstruction(event.target.value)}
                  aria-label="交接指令"
                  rows={3}
                  placeholder="只写目标 Agent 需要继续执行的 bounded instruction"
                />
              </label>
            </div>
            {errorMessage ? <div className="handoff-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{errorMessage}</div> : null}
          </div>
          <div className="handoff-dialog__actions">
            <button className="quiet-button" type="button" onClick={onClose} disabled={status !== "idle"}>取消</button>
            <button className="primary-button" type="button" onClick={() => void prepare()} disabled={!selectedCandidate || reason.trim() === "" || instruction.trim() === "" || status !== "idle"}>
              {status === "submitting" ? "生成中" : "查看切换方案"}
            </button>
          </div>
        </section>
      ) : null}
      {step === "review" ? (
        <ReviewDialog
          proposal={legacyProposal}
          nativeProposal={nativeProposal}
          isPending={proposalQuery.isPending}
          error={proposalQuery.error}
          status={status}
          errorMessage={errorMessage}
          dialogRef={dialogRef}
          onClose={onClose}
          onBack={() => {
            setStep("select");
            setErrorMessage(null);
          }}
          onCancel={() => {
            if (legacyProposal) void cancel(legacyProposal);
          }}
          onConfirm={() => {
            if (legacyProposal) void confirm(legacyProposal);
          }}
        />
      ) : null}
      {step === "progress" && transitionId ? (
        <section className="handoff-dialog handoff-dialog--progress" role="dialog" aria-modal="true" aria-label="Agent 切换进度" ref={dialogRef} tabIndex={-1}>
          <HandoffProgress
            workspaceId={workspace.workspace_id}
            transitionId={transitionId}
            onRefresh={onRefresh}
            onClose={onClose}
          />
        </section>
      ) : null}
    </div>
  );
}

function DialogHeading({
  title,
  eyebrow,
  onClose,
}: {
  title: string;
  eyebrow: string;
  onClose: () => void;
}) {
  return (
    <div className="handoff-dialog__heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2 id="handoff-dialog-title">{title}</h2>
      </div>
      <button className="icon-button" type="button" aria-label="关闭 Agent 切换窗口" title="关闭 Agent 切换窗口" onClick={onClose}>
        <X size={16} aria-hidden="true" />
      </button>
    </div>
  );
}

function HandoffScope({
  taskTitle,
  subtaskTitle,
  workspace,
}: {
  taskTitle: string;
  subtaskTitle: string;
  workspace: WorkerWorkspace;
}) {
  return (
    <div className="handoff-dialog__scope">
      <span className="eyebrow">影响范围</span>
      <strong>{taskTitle} / {subtaskTitle} / {workspace.worker_item.title}</strong>
      <span>只改变这个 WorkerItem 的执行 Agent，Task 和 Subtask 排布保持不变。</span>
    </div>
  );
}

function CandidateCard({
  candidate,
  selected,
  onSelect,
}: {
  candidate: HandoffCandidate;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <label className={`handoff-candidate${selected ? " is-selected" : ""}${candidate.eligible ? "" : " is-ineligible"}`}>
      <input
        type="radio"
        name="handoff-target-worker"
        value={candidate.worker_id}
        checked={selected}
        disabled={!candidate.eligible}
        onChange={onSelect}
        aria-label={`选择 ${candidate.display_name}`}
      />
      <span className="handoff-candidate__body">
        <span className="handoff-candidate__topline">
          <strong>{candidate.display_name}</strong>
          {candidate.recommended ? <span className="handoff-candidate__recommended"><CheckCircle2 size={12} aria-hidden="true" />推荐目标</span> : null}
        </span>
        <span>{candidate.adapter_type} · {candidate.runtime_profile_label ?? "未标注 profile"}</span>
        <small>{candidate.health_summary} · 能力满足 {candidate.capabilities_satisfied.join(" / ") || "无"}</small>
        {candidate.ineligibility_code ? <small className="handoff-candidate__reason">{candidate.ineligibility_code}</small> : null}
      </span>
      <span className={`handoff-candidate__state${candidate.eligible ? " is-eligible" : ""}`}>
        {candidate.eligible ? "可用" : "不可用"}
      </span>
    </label>
  );
}

function ReviewDialog({
  proposal,
  nativeProposal,
  isPending,
  error,
  status,
  errorMessage,
  dialogRef,
  onClose,
  onBack,
  onCancel,
  onConfirm,
}: {
  proposal: HandoffProposal | undefined;
  nativeProposal: NativeHandoffProposal | undefined;
  isPending: boolean;
  error: unknown;
  status: "idle" | "submitting" | "cancelling";
  errorMessage: string | null;
  dialogRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onBack: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const stale = proposal !== undefined && !canConfirmProposal(proposal);
  return (
    <section className="handoff-dialog" role="dialog" aria-modal="true" aria-labelledby="handoff-review-title" ref={dialogRef} tabIndex={-1}>
      <div className="handoff-dialog__heading">
        <div>
          <span className="eyebrow">WORKER HANDOFF / REVIEW</span>
          <h2 id="handoff-review-title">确认 Agent 切换</h2>
        </div>
        <button className="icon-button" type="button" aria-label="关闭 Agent 切换窗口" title="关闭 Agent 切换窗口" onClick={onClose} disabled={status !== "idle"}>
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      {nativeProposal ? (
        <div className="handoff-dialog__notice" role="status">Profile 原生交接方案请使用归属窗口复核</div>
      ) : null}
      {!nativeProposal && isPending ? <div className="handoff-dialog__notice" role="status">正在读取不可变切换方案</div> : null}
      {!nativeProposal && error ? <div className="handoff-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{describeError(error)}</div> : null}
      {!nativeProposal && proposal ? (
        <div className="handoff-dialog__body">
          {stale ? <div className="handoff-dialog__stale" role="alert"><CircleAlert size={15} aria-hidden="true" /><span><strong>方案已失效</strong>{proposal.stale_reason ? `：${proposal.stale_reason}` : ""}</span></div> : null}
          <section className="handoff-review-scope">
            <span className="eyebrow">IMMUTABLE SCOPE</span>
            <dl className="handoff-dialog__facts">
              <div><dt>Task</dt><dd>{proposal.task_title ?? proposal.root_task_id}</dd></div>
              <div><dt>Subtask</dt><dd>{proposal.subtask_title ?? proposal.subtask_id}</dd></div>
              <div><dt>WorkerItem</dt><dd><strong>{proposal.worker_item.title}</strong><span> · {proposal.worker_item.worker_item_id}</span></dd></div>
            </dl>
          </section>
          <section className="handoff-review-participants">
            <ParticipantFact label="Source Agent" participant={proposal.source} />
            <ArrowRight size={17} aria-hidden="true" />
            <ParticipantFact label="Target Agent" participant={proposal.target} />
          </section>
          <dl className="handoff-dialog__facts handoff-dialog__facts--grid">
            <div><dt>Workspace</dt><dd><span>{workspaceTransferLabel(proposal.transfer_mode)} · </span><code>{proposal.transfer_mode}</code><span> · manifest V{proposal.workspace.source_manifest_version} → V{proposal.workspace.target_manifest_version ?? "--"}</span></dd></div>
            <div><dt>Context</dt><dd>package V{proposal.context.source_context_version} · {proposal.context.source_package_hash}</dd></div>
            <div><dt>Plan base</dt><dd>GEN {proposal.plan_base.generation} · head V{proposal.plan_base.head_version}</dd></div>
            <div><dt>Proposal hash</dt><dd className="mono-value">{proposal.proposal_hash}</dd></div>
            <div><dt>Input plan</dt><dd>{formatJson(proposal.input_plan)}</dd></div>
            <div><dt>Input impact</dt><dd>{formatJson(proposal.impact_summary)}</dd></div>
          </dl>
          <div className="handoff-review-request">
            <div><span>切换原因</span><p>{proposal.handoff_reason}</p></div>
            <div><span>交接指令</span><p>{proposal.handoff_instruction}</p></div>
          </div>
          {errorMessage ? <div className="handoff-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{errorMessage}</div> : null}
        </div>
      ) : null}
      <div className="handoff-dialog__actions">
        {nativeProposal ? (
          <button className="quiet-button" type="button" onClick={onClose}>关闭</button>
        ) : (
          <>
            <button className="quiet-button" type="button" onClick={onBack} disabled={status !== "idle"}>返回选择</button>
            {proposal ? <button className="quiet-button" type="button" onClick={onCancel} disabled={status !== "idle"}>{status === "cancelling" ? "取消中" : "取消方案"}</button> : null}
            <button className="primary-button" type="button" onClick={onConfirm} disabled={!proposal || stale || status !== "idle"}>
              {status === "submitting" ? "确认中" : "确认切换"}
            </button>
          </>
        )}
      </div>
    </section>
  );
}

function ParticipantFact({
  label,
  participant,
}: {
  label: string;
  participant: HandoffProposal["source"];
}) {
  return (
    <div className="handoff-review-participant">
      <span className="eyebrow">{label === "Source Agent" ? "来源" : "目标"}</span>
      <strong>{participant.worker.display_name}</strong>
      <span>{participant.worker.adapter_type} · {participant.worker.runtime_profile_label ?? "未标注 profile"}</span>
      <small className="mono-label">Worker {participant.worker.worker_id}</small>
    </div>
  );
}

function canConfirmProposal(proposal: HandoffProposal): boolean {
  return proposal.status === "prepared";
}

function workspaceTransferLabel(mode: string): string {
  return mode === "reuse_exact" ? "复用同一 Workspace" : mode;
}

function formatJson(value: Record<string, unknown>): string {
  const serialized = JSON.stringify(value);
  return serialized === "{}" || serialized === undefined ? "未额外指定" : serialized;
}

function describeError(error: unknown): string {
  if (error instanceof ApiProblem) return error.detail;
  return error instanceof Error ? error.message : "请求未能完成";
}
