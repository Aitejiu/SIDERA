import {
  CircleAlert,
  CircleCheck,
  Pin,
  RefreshCcw,
  Route,
  ServerCog,
  ShieldAlert,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { ApiProblem } from "@/shared/api/client";

import { taskGraphQueryKey } from "../tasks/api";
import { executionQueryKey } from "../trace/api";
import { subtaskWorkspaceQueryKey } from "../subtasks/api";
import {
  listWorkers,
  workersQueryKey,
} from "../workers/api";
import type { Worker } from "../workers/types";
import {
  clearExecutionWorkerPin,
  pinExecutionWorker,
  workerActivityQueryKey,
  workerWorkspaceQueryKey,
} from "./api";
import type { WorkerExecution } from "./types";

type CandidateState = {
  selectable: boolean;
  reason: string;
  isMissingRuntime: boolean;
};

export function ExecutionRoutingPanel({
  workspaceId,
  execution,
  workerItemId,
  rootTaskId,
  subtaskKey,
  onRefresh,
}: {
  workspaceId: string;
  execution: WorkerExecution | null;
  workerItemId?: string;
  rootTaskId?: string;
  subtaskKey?: string;
  onRefresh: () => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const [chooserOpen, setChooserOpen] = useState(false);
  const [selectedWorkerId, setSelectedWorkerId] = useState<string | null>(null);
  const [mutationState, setMutationState] = useState<"idle" | "saving">("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const isQueued = execution?.status === "queued";
  const isTerminal = execution?.scheduling_status === "terminal"
    || ["succeeded", "failed", "canceled", "lost"].includes(execution?.status ?? "");

  const workersQuery = useQuery({
    queryKey: workersQueryKey(workspaceId),
    queryFn: ({ signal }) => listWorkers(workspaceId, signal),
    enabled: chooserOpen && isQueued,
  });

  const selectedWorker = useMemo(
    () => workersQuery.data?.find((worker) => worker.id === selectedWorkerId) ?? null,
    [selectedWorkerId, workersQuery.data],
  );
  const selectedState = selectedWorker && execution
    ? candidateState(selectedWorker, execution)
    : null;

  if (execution === null) {
    return (
      <section className="worker-item-routing" aria-labelledby="worker-item-routing-title">
        <RoutingHeading />
        <p className="worker-item-routing__empty">当前没有可调整的执行尝试。</p>
      </section>
    );
  }

  const currentWorker = execution.assigned_worker_summary ?? execution.pinned_worker_summary;
  const isImmutable = !isQueued;

  const openChooser = () => {
    setMessage(null);
    setErrorMessage(null);
    setSelectedWorkerId(null);
    setChooserOpen(true);
  };

  const closeChooser = () => {
    if (mutationState === "saving") return;
    setChooserOpen(false);
    setSelectedWorkerId(null);
    setErrorMessage(null);
  };

  const applyCommand = async (
    command: () => Promise<unknown>,
    successMessage: string,
  ) => {
    setMutationState("saving");
    setMessage(null);
    setErrorMessage(null);
    try {
      await command();
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
      await invalidateRoutingQueries({
        queryClient,
        workspaceId,
        executionId: execution.execution_id,
        workerItemId,
        rootTaskId,
        subtaskKey,
      });
      try {
        await onRefresh();
      } catch {
        // The command already succeeded; the stream or projection can refresh later.
      }
      setChooserOpen(false);
      setSelectedWorkerId(null);
      setMessage(successMessage);
    } catch (error) {
      setErrorMessage(
        error instanceof ApiProblem && error.status === 409
          ? "执行状态已变化，请刷新后重新确认"
          : describeError(error),
      );
      setChooserOpen(true);
    } finally {
      setMutationState("idle");
    }
  };

  const handlePin = () => {
    if (selectedWorker === null || selectedState === null || !selectedState.selectable) return;
    void applyCommand(
      () => pinExecutionWorker(
        workspaceId,
        execution.execution_id,
        execution.version,
        selectedWorker.id,
      ),
      `已固定到 ${selectedWorker.display_name}`,
    );
  };

  const handleClear = () => {
    void applyCommand(
      () => clearExecutionWorkerPin(
        workspaceId,
        execution.execution_id,
        execution.version,
      ),
      "已恢复自动选择",
    );
  };

  return (
    <section className="worker-item-routing" aria-labelledby="worker-item-routing-title">
      <RoutingHeading />
      <div className="worker-item-routing__attempt">
        <div className="worker-item-routing__attempt-line">
          <strong>执行尝试 #{execution.attempt}</strong>
          <span className="worker-item-routing__version">V{execution.version}</span>
        </div>
        <div className="worker-item-routing__mode">
          <Route size={14} aria-hidden="true" />
          <strong>{modeLabel(execution)}</strong>
          <span>{statusLabel(execution, isTerminal)}</span>
        </div>
      </div>

      {execution.scheduling_status === "waiting" && execution.waiting_details_summary ? (
        <div className="worker-item-routing__wait" role="status">
          <CircleAlert size={14} aria-hidden="true" />
          <span>{execution.waiting_details_summary}</span>
          {execution.requires_operator ? <strong>需要处理</strong> : null}
        </div>
      ) : null}

      {isImmutable ? (
        <div className="worker-item-routing__immutable" role="note">
          <ShieldAlert size={14} aria-hidden="true" />
          <div>
            <strong>{currentWorker?.display_name ?? "已绑定执行节点"}</strong>
            <span>当前执行已绑定；更换节点需要停止并创建新的执行尝试</span>
          </div>
        </div>
      ) : (
        <div className="worker-item-routing__actions">
          {execution.pinned_worker_summary ? (
            <button
              className="quiet-button"
              type="button"
              onClick={handleClear}
              disabled={mutationState === "saving"}
            >
              <RefreshCcw size={14} aria-hidden="true" />
              恢复自动选择
            </button>
          ) : null}
          <button
            className="quiet-button"
            type="button"
            onClick={chooserOpen ? closeChooser : openChooser}
            disabled={mutationState === "saving"}
          >
            {chooserOpen ? <X size={14} aria-hidden="true" /> : <ServerCog size={14} aria-hidden="true" />}
            {chooserOpen ? "关闭选择器" : "调整执行节点"}
          </button>
        </div>
      )}

      {message ? (
        <div className="worker-item-routing__message" role="status">
          <CircleCheck size={14} aria-hidden="true" />
          {message}
        </div>
      ) : null}

      {chooserOpen && !isImmutable ? (
        <div className="worker-item-routing__chooser" role="region" aria-label="执行节点选择器">
          <div className="worker-item-routing__chooser-heading">
            <div>
              <strong>选择基础设施执行节点</strong>
              <span>只影响这次执行尝试，不会替换 WorkerItem。</span>
            </div>
            <span className="mono-label">EXECUTION V{execution.version}</span>
          </div>
          {workersQuery.isPending ? <p className="muted-copy">读取执行节点状态</p> : null}
          {workersQuery.isError ? (
            <div className="worker-item-routing__error" role="alert">无法读取执行节点，请稍后重试。</div>
          ) : null}
          <div className="worker-item-routing__candidates">
            {workersQuery.data?.map((worker) => {
              const state = candidateState(worker, execution);
              const selected = selectedWorkerId === worker.id;
              return (
                <button
                  className={`worker-item-candidate${selected ? " is-selected" : ""}`}
                  key={worker.id}
                  type="button"
                  disabled={!state.selectable || mutationState === "saving"}
                  aria-label={`${worker.display_name}${state.reason === "可用" ? "" : ` · ${state.reason}`}`}
                  onClick={() => {
                    setSelectedWorkerId(worker.id);
                    setErrorMessage(null);
                  }}
                >
                  <span className="worker-item-candidate__main">
                    <strong>{worker.display_name}</strong>
                    <small>{worker.adapter_type} · {worker.current_load}/{worker.capacity}</small>
                  </span>
                  <span className={`worker-item-candidate__state${state.selectable ? " is-selectable" : ""}`}>
                    {state.reason}
                  </span>
                </button>
              );
            })}
          </div>
          {selectedState?.isMissingRuntime ? (
            <p className="worker-item-routing__notice">
              该执行会等待节点完成运行配置
            </p>
          ) : null}
          {errorMessage ? <div className="worker-item-routing__error" role="alert">{errorMessage}</div> : null}
          <div className="worker-item-routing__chooser-actions">
            <button className="quiet-button" type="button" onClick={closeChooser} disabled={mutationState === "saving"}>
              取消
            </button>
            <button
              className="dispatch-button"
              type="button"
              onClick={handlePin}
              disabled={selectedWorker === null || selectedState?.selectable !== true || mutationState === "saving"}
            >
              <Pin size={14} aria-hidden="true" />
              {mutationState === "saving" ? "正在保存节点选择" : "固定到此节点"}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function RoutingHeading() {
  return (
    <div className="worker-item-routing__heading">
      <h3 id="worker-item-routing-title"><Route size={15} aria-hidden="true" />执行尝试</h3>
      <span className="eyebrow">ROUTING</span>
    </div>
  );
}

function modeLabel(execution: WorkerExecution): string {
  if (execution.pinned_worker_summary) return `固定到 ${execution.pinned_worker_summary.display_name}`;
  if (execution.pinned_worker_id) return `固定节点 ${execution.pinned_worker_id.slice(0, 8)}`;
  return "自动选择";
}

function statusLabel(execution: WorkerExecution, isTerminal: boolean): string {
  if (isTerminal) return "已结束";
  if (execution.scheduling_status === "waiting") return "等待调度";
  if (execution.status === "queued") return "排队中";
  if (execution.status === "leased") return "已租约";
  return "执行中";
}

function candidateState(worker: Worker, execution: WorkerExecution): CandidateState {
  const required = new Set(execution.capability_requirements);
  const capabilities = new Set(worker.capabilities);
  if ([...required].some((capability) => !capabilities.has(capability))) {
    return { selectable: false, reason: "能力不兼容", isMissingRuntime: false };
  }
  if (execution.adapter_forbidden.includes(worker.adapter_type)) {
    return { selectable: false, reason: "Adapter 被策略禁止", isMissingRuntime: false };
  }
  if (worker.status === "offline" || worker.status === "unhealthy") {
    return { selectable: false, reason: "节点离线", isMissingRuntime: false };
  }
  if (worker.desired_state === "draining") {
    return { selectable: false, reason: "节点排空中", isMissingRuntime: false };
  }
  if (worker.current_load >= worker.capacity) {
    return { selectable: false, reason: "容量已满", isMissingRuntime: false };
  }
  if (worker.runtime_configuration_status === "invalid") {
    return { selectable: false, reason: "运行配置失效", isMissingRuntime: false };
  }
  if (worker.runtime_configuration_status === "missing") {
    return { selectable: true, reason: "运行配置未配置", isMissingRuntime: true };
  }
  return { selectable: true, reason: "可用", isMissingRuntime: false };
}

async function invalidateRoutingQueries({
  queryClient,
  workspaceId,
  executionId,
  workerItemId,
  rootTaskId,
  subtaskKey,
}: {
  queryClient: QueryClient;
  workspaceId: string;
  executionId: string;
  workerItemId: string | undefined;
  rootTaskId: string | undefined;
  subtaskKey: string | undefined;
}) {
  const invalidations = [
    queryClient.invalidateQueries({ queryKey: workersQueryKey(workspaceId) }),
    queryClient.invalidateQueries({ queryKey: executionQueryKey(workspaceId, executionId) }),
  ];
  if (workerItemId) {
    invalidations.push(
      queryClient.invalidateQueries({ queryKey: workerWorkspaceQueryKey(workspaceId, workerItemId) }),
      queryClient.invalidateQueries({ queryKey: workerActivityQueryKey(workspaceId, workerItemId) }),
    );
  }
  if (rootTaskId) {
    invalidations.push(queryClient.invalidateQueries({ queryKey: taskGraphQueryKey(workspaceId, rootTaskId) }));
  }
  if (rootTaskId && subtaskKey) {
    invalidations.push(
      queryClient.invalidateQueries({ queryKey: subtaskWorkspaceQueryKey(workspaceId, rootTaskId, subtaskKey) }),
    );
  }
  await Promise.all(invalidations);
}

function describeError(error: unknown): string {
  if (error instanceof ApiProblem) return error.detail;
  return "保存节点选择失败，请稍后重试。";
}
