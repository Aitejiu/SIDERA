import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquare, RefreshCw, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  getWorkContextByPlanningRequest, getWorkContextByRootTask,
  workContextByPlanningRequestQueryKey, workContextByRootTaskQueryKey,
} from "@/features/context/api";
import type { LocalSession } from "@/features/local-session/api";
import { planningRequestQueryKey, planningRunsQueryKey } from "./api";
import {
  failedPlanningFeedbackPayload, loadFailedPlanningFeedbackDraft,
  reviewFailedPlanningFeedback, saveFailedPlanningFeedbackDraft,
} from "./failedPlanningFeedback.js";
import type { FailedPlanningFeedbackBasis } from "./failedPlanningFeedback.js";
import type { PlanningDraftScope } from "./planningConfirmationDraft";
import type { PlanningRequest, PlanningRun, PlanningRunPage, ReviseFailedPlanningRequestPayload } from "./types";
import type { useNativePlanningCommand } from "./useNativePlanningCommand";

interface Props {
  scope: PlanningDraftScope | null;
  session: LocalSession | null;
  request: PlanningRequest;
  runs: readonly PlanningRun[] | undefined;
  command: Pick<ReturnType<typeof useNativePlanningCommand>, "original" | "status" | "busy" | "unresolved">;
  disabledReason: string | undefined;
  onSubmit(expectedVersion: number, payload: ReviseFailedPlanningRequestPayload): Promise<void>;
  onRefresh(): Promise<void>;
}

interface Editor {
  basis: FailedPlanningFeedbackBasis;
  text: string;
  error: string | null;
  readFailed: boolean;
}

export function FailedPlanningFeedback(props: Props) {
  const { request, session, scope } = props;
  const identity = JSON.stringify([request.kind, request.workspace_id, request.id, session?.principal_id,
    scope?.kind === "revision" ? scope.root_task_id : request.revision_base?.root_task_id]);
  return <FeedbackEditor key={identity} {...props} />;
}

function FeedbackEditor({ scope, session, request, runs, command, disabledReason, onSubmit, onRefresh }: Props) {
  const queryClient = useQueryClient();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const actionRef = useRef<HTMLButtonElement>(null);
  const handledRejected = useRef(new Set<string>());
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const sessionValid = validSession(scope, session);
  const enabled = sessionValid && scope !== null && request.kind === scope.kind
    && request.id === scope.request_id && request.workspace_id === scope.workspace_id
    && request.status === "failed" && request.profile?.execution_mode === "native_profile";
  const contextKey = scope?.kind === "revision"
    ? workContextByRootTaskQueryKey(scope.workspace_id, scope.root_task_id)
    : workContextByPlanningRequestQueryKey(request.workspace_id, request.id);
  const contextQuery = useQuery({
    queryKey: contextKey,
    queryFn: ({ signal }) => scope?.kind === "revision"
      ? getWorkContextByRootTask(scope.workspace_id, scope.root_task_id, signal)
      : getWorkContextByPlanningRequest(request.workspace_id, request.id, signal),
    enabled,
    retry: false,
  });
  const review = scope === null ? null : reviewFailedPlanningFeedback({ scope, request, runs, context: contextQuery.data });
  const latest = useRef({ scope, session, review, onRefresh });
  useLayoutEffect(() => { latest.current = { scope, session, review, onRefresh }; }, [scope, session, review, onRefresh]);

  const original = command.original;
  useEffect(() => {
    if (scope === null || original?.command_type !== "ReviseFailedPlanningRequest" || command.status !== "rejected"
      || original.workspace_id !== scope.workspace_id || original.target_id !== scope.request_id
      || original.actor.id !== scope.principal_id || handledRejected.current.has(original.command_id)) return;
    handledRejected.current.add(original.command_id);
    void latest.current.onRefresh().catch(() => {
      if (mounted.current) setReadError("无法刷新规划事实，请重新核对");
    });
  }, [command.status, original, scope]);

  const stale = editor !== null && (review?.ok !== true || !sameBasis(editor.basis, review.basis));
  const appliedToCurrentRun = command.status === "applied" && original?.command_type === "ReviseFailedPlanningRequest"
    && review?.ok === true && original.payload.failed_planning_run_id === review.basis.failedRunId
    && original.payload.expected_failed_planning_run_version === review.basis.failedRunVersion;
  const reason = disabledReason ?? readError ?? (!sessionValid ? "需要当前工作区的有效本地会话"
    : command.busy || command.unresolved ? "请先核对原始命令结果"
      : refreshing || contextQuery.isFetching ? "正在读取需求上下文"
        : contextQuery.isError ? "无法读取需求上下文，请重新读取"
          : appliedToCurrentRun ? "补充要求已处理，请重新读取规划事实"
            : review?.ok !== true ? review?.reason ?? "无法核实规划依据"
              : stale ? "规划依据已变化，请重新核对" : null);

  function openEditor() {
    if (reason !== null || review?.ok !== true) return;
    const loaded = loadFailedPlanningFeedbackDraft(review.basis);
    setEditor({ basis: review.basis, text: loaded.ok ? loaded.feedback : "",
      error: loaded.ok ? null : loaded.reason, readFailed: !loaded.ok });
  }

  function changeText(text: string) {
    if (editor === null || editor.readFailed || command.busy || command.unresolved) return;
    const saved = saveFailedPlanningFeedbackDraft(editor.basis, text);
    setEditor({ ...editor, text, error: saved.ok ? null : saved.reason });
  }

  async function recheck() {
    if (!validSession(scope, session) || scope === null || command.busy || command.unresolved || refreshing) return;
    const prior = editor;
    setRefreshing(true);
    setReadError(null);
    try {
      await onRefresh();
      if (!mounted.current || !validSession(latest.current.scope, latest.current.session)) return;
      const contextRead = await contextQuery.refetch();
      if (!mounted.current || contextRead.isError || contextRead.data === undefined) {
        if (mounted.current) setReadError("无法读取需求上下文，请重新核对");
        return;
      }
      const nextRequest = queryClient.getQueryData<PlanningRequest>(planningRequestQueryKey(scope.workspace_id, scope.request_id)) ?? request;
      const nextRuns = queryClient.getQueryData<PlanningRunPage>(planningRunsQueryKey(scope.workspace_id, scope.request_id))?.items ?? runs;
      const next = reviewFailedPlanningFeedback({ scope, request: nextRequest, runs: nextRuns, context: contextRead.data });
      if (!next.ok) { setReadError(next.reason); return; }
      if (prior !== null) {
        const restored = prior.readFailed ? loadFailedPlanningFeedbackDraft(prior.basis) : { ok: true as const, feedback: prior.text };
        if (!restored.ok) { setReadError(restored.reason); return; }
        const saved = saveFailedPlanningFeedbackDraft(next.basis, restored.feedback);
        setEditor({ basis: next.basis, text: restored.feedback, error: saved.ok ? null : saved.reason, readFailed: false });
      }
    } catch {
      if (mounted.current) setReadError("无法读取规划事实，请重新核对");
    } finally {
      if (mounted.current) setRefreshing(false);
    }
  }

  async function submit() {
    const current = latest.current;
    if (editor === null || reason !== null || editor.error !== null || editor.readFailed
      || !validSession(current.scope, current.session) || current.review?.ok !== true
      || !sameBasis(editor.basis, current.review.basis) || editor.text.trim().length === 0) return;
    const saved = saveFailedPlanningFeedbackDraft(editor.basis, editor.text);
    if (!saved.ok) { setEditor({ ...editor, error: saved.reason }); return; }
    try {
      await onSubmit(editor.basis.requestVersion, failedPlanningFeedbackPayload(editor.basis, editor.text));
    } catch {
      if (mounted.current) setReadError("补充要求未能提交，请核对原始命令");
    }
  }

  return (
    <section className="planning-feedback-composer" aria-label="补充规划要求">
      {editor === null ? (
        <button ref={actionRef} className="quiet-button" type="button" disabled={reason !== null} onClick={openEditor}>
          <MessageSquare size={14} aria-hidden="true" />补充要求并重新规划
        </button>
      ) : (
        <>
          <div className="planning-section-heading">
            <h2>补充规划要求</h2>
            <span className="mono-label">失败运行 #{editor.basis.attempt} · Brief R{editor.basis.briefRevision}</span>
          </div>
          <label className="planning-field">
            <span>补充要求</span>
            <textarea autoFocus aria-label="补充要求" rows={4} value={editor.text}
              readOnly={command.busy || command.unresolved || editor.readFailed || refreshing}
              onChange={(event) => changeText(event.target.value)} />
          </label>
          {editor.error ? <p className="planning-form-error" role="alert">{editor.error}</p> : null}
          <button className="quiet-button" type="button"
            disabled={reason !== null || editor.error !== null || editor.readFailed || editor.text.trim().length === 0}
            aria-busy={command.busy} onClick={() => void submit()}>
            <MessageSquare size={14} aria-hidden="true" />{command.busy ? "正在提交" : "补充要求并重新规划"}
          </button>
          <button className="quiet-button" type="button" disabled={command.busy || command.unresolved}
            onClick={() => { setEditor(null); requestAnimationFrame(() => actionRef.current?.focus()); }}>
            <X size={14} aria-hidden="true" />取消
          </button>
        </>
      )}
      {reason ? <p role={contextQuery.isError ? "alert" : "status"}>{reason}</p> : null}
      {readError ? <p role="alert">{readError}</p> : null}
      {(reason !== null || editor !== null || readError !== null) ? (
        <button className="quiet-button" type="button" disabled={!sessionValid || command.busy || command.unresolved || refreshing}
          onClick={() => void recheck()}><RefreshCw size={14} aria-hidden="true" />{editor !== null ? "重新核对" : "重新读取"}</button>
      ) : null}
    </section>
  );
}

function validSession(scope: PlanningDraftScope | null, session: LocalSession | null): boolean {
  return scope !== null && session !== null && session.workspace_id === scope.workspace_id
    && session.principal_id === scope.principal_id && session.session_id.trim().length > 0
    && Number.isFinite(Date.parse(session.expires_at)) && Date.parse(session.expires_at) > Date.now();
}

function sameBasis(left: FailedPlanningFeedbackBasis, right: FailedPlanningFeedbackBasis): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}
