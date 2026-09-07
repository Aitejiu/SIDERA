import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Download, FileArchive, RefreshCw } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiProblem, createCommandIdentity } from "@/shared/api/client";

import {
  executeTaskResultAction,
  getTaskResultAcceptanceBasis,
  getTaskResults,
  taskResultsQueryKey,
  type AcceptanceCriterionDecision,
  type TaskResultOverview,
} from "./api";
import { AcceptanceForm } from "./AcceptanceForm";
import { ResultHistory } from "./ResultHistory";

export function TaskResultPanel({
  workspaceId,
  rootTaskId,
}: {
  workspaceId: string;
  rootTaskId: string;
}) {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const resultQuery = useQuery({
    queryKey: taskResultsQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getTaskResults(workspaceId, rootTaskId, { signal }),
    enabled: Boolean(workspaceId && rootTaskId),
  });

  if (resultQuery.isPending) {
    return <section className="task-result-panel" aria-label="成果验收"><div className="task-result-panel__state" role="status">正在读取成果</div></section>;
  }
  if (resultQuery.isError || resultQuery.data === undefined) {
    return (
      <section className="task-result-panel" aria-label="成果验收">
        <div className="task-result-panel__state task-result-panel__state--error" role="alert">
          <strong>无法读取成果</strong>
          <p>控制面没有返回当前根任务的成果快照。</p>
          <button className="quiet-button" type="button" onClick={() => void resultQuery.refetch()}>
            <RefreshCw size={14} aria-hidden="true" />重新读取成果
          </button>
        </div>
      </section>
    );
  }

  const view = resultQuery.data;
  const currentId = view.current_candidate?.candidate_id ?? "";
  const selectedId = searchParams.get("candidate") ?? currentId;
  const selectedCandidate = view.candidate_history.items.find((item) => item.candidate_id === selectedId)
    ?? view.current_candidate
    ?? undefined;
  const historical = selectedCandidate !== undefined && selectedCandidate.candidate_id !== currentId;
  const selectCandidate = (candidateId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("view", "results");
    next.set("candidate", candidateId);
    setSearchParams(next);
  };

  return (
    <section className="task-result-panel" aria-label="成果验收">
      <div className="task-result-panel__heading">
        <div>
          <span className="eyebrow">TASK RESULT</span>
          <h2>成果验收</h2>
          <p>只审阅当前成果版本；根任务完成以实际验收和收尾事实为准。</p>
        </div>
        <StageMark stage={view.result_stage} />
      </div>
      {view.current_candidate === null ? (
        <EmptyResultState view={view} />
      ) : selectedCandidate === undefined ? (
        <div className="task-result-panel__state" role="status">指定的成果版本不存在，已保留当前结果快照。</div>
      ) : (
        <>
          <CandidateHeader candidate={selectedCandidate} historical={historical} />
          {view.blocking_reasons.length > 0 ? (
            <div className="task-result-panel__blocking" role="note">
              <AlertTriangle size={15} aria-hidden="true" />
              <div>{view.blocking_reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>
            </div>
          ) : null}
          <CriterionEvidence view={view} />
          {!historical && canAccept(view) ? (
            <AcceptanceSection
              workspaceId={workspaceId}
              rootTaskId={rootTaskId}
              view={view}
              onAccepted={() => void queryClient.invalidateQueries({ queryKey: taskResultsQueryKey(workspaceId, rootTaskId) })}
            />
          ) : historical ? (
            <div className="task-result-panel__readonly" role="note">历史候选，只读</div>
          ) : null}
          <ArtifactList workspaceId={workspaceId} artifacts={selectedCandidate.artifacts} />
        </>
      )}
      <ResultHistory view={view} selectedCandidateId={selectedCandidate?.candidate_id ?? selectedId} onSelect={selectCandidate} />
    </section>
  );
}

function AcceptanceSection({
  workspaceId,
  rootTaskId,
  view,
  onAccepted,
}: {
  workspaceId: string;
  rootTaskId: string;
  view: TaskResultOverview;
  onAccepted: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const action = view.available_actions.find((candidate) => candidate.command_type === "CompleteTask");
  if (action === undefined) return null;
  const acceptAction = action;

  async function submit(criteria: AcceptanceCriterionDecision[]) {
    setPending(true);
    setError(null);
    const identity = createCommandIdentity();
    try {
      const basis = await getTaskResultAcceptanceBasis(
        workspaceId,
        rootTaskId,
        acceptAction.candidate_id ?? "",
        criteria,
      );
      await executeTaskResultAction(
        workspaceId,
        acceptAction,
        {
          command_type: "CompleteTask",
          payload: {
            candidate_id: basis.candidate_id,
            candidate_hash: basis.candidate_hash,
            acceptance_basis_hash: basis.acceptance_basis_hash,
            expected_result_version: basis.expected_result_version,
            criteria,
          },
        },
        identity,
      );
      onAccepted();
    } catch (cause) {
      setError(cause instanceof ApiProblem && cause.status === 409
        ? "成果已变化，请重新审阅"
        : cause instanceof ApiProblem ? cause.detail : "验收请求没有完成，请重新读取当前成果。");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="task-result-panel__acceptance">
      {error ? <div className="task-result-panel__error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</div> : null}
      <AcceptanceForm key={`${view.root_task_id}:${view.result_version}`} view={view} action={action} onSubmit={submit} pending={pending} />
    </div>
  );
}

function CandidateHeader({
  candidate,
  historical,
}: {
  candidate: NonNullable<TaskResultOverview["current_candidate"]>;
  historical: boolean;
}) {
  return (
    <div className="task-result-panel__candidate">
      <div>
        <span className="eyebrow">{historical ? "HISTORICAL CANDIDATE" : "CURRENT CANDIDATE"}</span>
        <strong>{shortHash(candidate.candidate_hash)}</strong>
      </div>
      <dl>
        <div><dt>基线</dt><dd>{shortHash(candidate.base_commit)}</dd></div>
        <div><dt>结果</dt><dd>{candidate.result_commit ? shortHash(candidate.result_commit) : "未生成"}</dd></div>
        <div><dt>计划</dt><dd>{shortHash(candidate.plan_content_hash)}</dd></div>
      </dl>
    </div>
  );
}

function CriterionEvidence({ view }: { view: TaskResultOverview }) {
  return (
    <section className="task-result-panel__evidence" aria-labelledby="result-evidence-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">EVIDENCE</span>
          <h3 id="result-evidence-title">验收标准与检查事实</h3>
        </div>
        <span className="mono-label">CURSOR {view.last_change_cursor}</span>
      </div>
      <div className="task-result-panel__criteria">
        {view.criteria.map((criterion) => (
          <article key={criterion.criterion_key}>
            <div>
              <strong>{criterion.text}</strong>
              <span>{criterion.mode === "automatic" ? "自动标准" : "人工标准"} · {criterion.check_keys.join("、") || "无关联检查"}</span>
            </div>
            <span className={`result-acceptance-status result-acceptance-status--${criterion.observed_status}`}>
              {criterionStatusLabel(criterion.observed_status)}
            </span>
          </article>
        ))}
      </div>
    </section>
  );
}

function ArtifactList({
  workspaceId,
  artifacts,
}: {
  workspaceId: string;
  artifacts: NonNullable<TaskResultOverview["current_candidate"]>["artifacts"];
}) {
  return (
    <section className="task-result-panel__artifacts" aria-labelledby="result-artifacts-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">DELIVERABLES</span>
          <h3 id="result-artifacts-title">成果获取</h3>
        </div>
      </div>
      {artifacts.length === 0 ? <p className="muted-copy">当前候选没有登记成果文件。</p> : (
        <ul>
          {artifacts.map((artifact) => (
            <li key={artifact.artifact_id}>
              <FileArchive size={14} aria-hidden="true" />
              <span><strong>{artifact.artifact_type}</strong><small>{artifact.media_type} · {formatBytes(artifact.size_bytes)} · {shortHash(artifact.content_hash)}</small></span>
              {artifact.content_available ? (
                <Link className="quiet-button" to={`/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`}><Download size={13} aria-hidden="true" />查看 / 下载</Link>
              ) : <span className="muted-copy">内容不可用</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function EmptyResultState({ view }: { view: TaskResultOverview }) {
  return (
    <div className="task-result-panel__empty" role="status">
      <CheckCircle2 size={18} aria-hidden="true" />
      <strong>还没有可审阅的成果</strong>
      <p>{view.blocking_reasons[0] ?? "等待集成 Worker 产生当前成果候选。"}</p>
    </div>
  );
}

function StageMark({ stage }: { stage: string }) {
  return <span className={`task-result-stage task-result-stage--${stage}`}><CheckCircle2 size={14} aria-hidden="true" />{stageLabel(stage)}</span>;
}

function canAccept(view: TaskResultOverview): boolean {
  return view.acceptance === null
    && view.available_actions.some((action) => action.command_type === "CompleteTask");
}

function stageLabel(stage: string): string {
  return {
    awaiting_acceptance: "等待用户验收",
    verifying: "检查中",
    integrating: "集成中",
    reworking: "返工中",
    applying: "应用中",
    finalizing: "收尾中",
    completed: "已完成",
    legacy: "旧任务",
  }[stage] ?? stage;
}

function criterionStatusLabel(value: string): string {
  return { passed: "检查已通过", failed: "检查未通过", pending: "等待检查" }[value] ?? value;
}

function shortHash(value: string): string {
  return value.slice(0, 12);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
