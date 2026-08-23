import { CircleAlert, ExternalLink, Link2, LoaderCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";

import { getPlanningRequest, planningRequestQueryKey } from "@/features/planning/api";
import { getTaskGraph, taskGraphQueryKey } from "@/features/tasks/api";

export type ChatContextKind = "planning" | "task" | "subtask" | "worker-item";

export interface ChatContextReference {
  key: string;
  kind: ChatContextKind;
  resourceId: string;
  label: string;
  href: string;
}

export function collectContextReferences(
  contextRefs: string[],
  toolResourceRefs: (string | null)[],
  workspaceId: string,
): ChatContextReference[] {
  const references: ChatContextReference[] = [];
  const seen = new Set<string>();
  for (const raw of [...contextRefs, ...toolResourceRefs]) {
    if (raw === null) continue;
    const reference = parseContextReference(raw, workspaceId);
    if (reference === null || seen.has(reference.key)) continue;
    seen.add(reference.key);
    references.push(reference);
  }
  return references;
}

export function ChatContextPanel({
  workspaceId,
  references,
  taskLinkLabel = "打开任务控制面",
}: {
  workspaceId: string;
  references: ChatContextReference[];
  taskLinkLabel?: string;
}) {
  const planningQueries = useQueries({
    queries: references
      .filter((reference) => reference.kind === "planning")
      .map((reference) => ({
        queryKey: planningRequestQueryKey(workspaceId, reference.resourceId),
        queryFn: ({ signal }: { signal: AbortSignal }) => getPlanningRequest(workspaceId, reference.resourceId, signal),
      })),
  });
  const taskQueries = useQueries({
    queries: references
      .filter((reference) => reference.kind === "task")
      .map((reference) => ({
        queryKey: taskGraphQueryKey(workspaceId, reference.resourceId),
        queryFn: ({ signal }: { signal: AbortSignal }) => getTaskGraph(workspaceId, reference.resourceId, signal),
      })),
  });
  const planningQueryMap = new Map(
    references
      .filter((reference) => reference.kind === "planning")
      .map((reference, index) => [reference.key, planningQueries[index]]),
  );
  const taskQueryMap = new Map(
    references
      .filter((reference) => reference.kind === "task")
      .map((reference, index) => [reference.key, taskQueries[index]]),
  );

  return (
    <aside className="chat-context-panel" aria-label="对话上下文">
      <div className="chat-pane-heading">
        <div>
          <span className="eyebrow">CONTEXT</span>
          <h2>关联对象</h2>
        </div>
      </div>
      <div className="chat-context-panel__body">
        {references.length === 0 ? (
          <span className="chat-context-panel__muted">当前没有显式关联</span>
        ) : (
          references.map((reference) => (
            <ContextObject
              key={reference.key}
              reference={reference}
              planningQuery={planningQueryMap.get(reference.key)}
              taskQuery={taskQueryMap.get(reference.key)}
            />
          ))
        )}
      </div>
      <Link className="chat-context-panel__task-link" to={`/workspaces/${workspaceId}/tasks`}>
        <Link2 size={14} aria-hidden="true" />
        <span>{taskLinkLabel}</span>
      </Link>
    </aside>
  );
}

type PlanningQueryState = {
  isPending: boolean;
  isError: boolean;
  data: { status: string; version: number } | undefined;
};

type TaskQueryState = {
  isPending: boolean;
  isError: boolean;
  data: { overview: { title: string; completed_subtask_count: number; planned_subtask_count: number } } | undefined;
};

function ContextObject({
  reference,
  planningQuery,
  taskQuery,
}: {
  reference: ChatContextReference;
  planningQuery: PlanningQueryState | undefined;
  taskQuery: TaskQueryState | undefined;
}) {
  if (reference.kind === "planning") {
    return (
      <ContextObjectFrame reference={reference}>
        {planningQuery?.isPending ? <ContextLoading /> : planningQuery?.isError || planningQuery?.data === undefined ? <ContextUnavailable /> : (
          <>
            <strong>{planningQuery.data.status}</strong>
            <span className="mono-label">V{planningQuery.data.version} · {reference.resourceId}</span>
          </>
        )}
      </ContextObjectFrame>
    );
  }
  if (reference.kind === "task") {
    return (
      <ContextObjectFrame reference={reference}>
        {taskQuery?.isPending ? <ContextLoading /> : taskQuery?.isError || taskQuery?.data === undefined ? <ContextUnavailable /> : (
          <>
            <strong>{taskQuery.data.overview.title}</strong>
            <span>{taskQuery.data.overview.completed_subtask_count} / {taskQuery.data.overview.planned_subtask_count} 个子任务完成</span>
          </>
        )}
      </ContextObjectFrame>
    );
  }
  return (
    <ContextObjectFrame reference={reference}>
      <strong>{reference.kind === "subtask" ? "子任务" : "WorkerItem"}</strong>
      <span className="mono-label">{reference.resourceId}</span>
    </ContextObjectFrame>
  );
}

function ContextObjectFrame({
  reference,
  children,
}: {
  reference: ChatContextReference;
  children: React.ReactNode;
}) {
  return (
    <div className="chat-context-object">
      <div className="chat-context-object__heading">
        <span className="eyebrow">{reference.label}</span>
        <Link to={reference.href} aria-label={`打开${reference.label}`} title={`打开${reference.label}`}>
          <ExternalLink size={13} aria-hidden="true" />
        </Link>
      </div>
      <div className="chat-context-object__body">{children}</div>
    </div>
  );
}

function ContextLoading() {
  return <span className="chat-context-panel__muted"><LoaderCircle size={13} aria-hidden="true" />正在读取</span>;
}

function ContextUnavailable() {
  return <span className="chat-context-panel__muted"><CircleAlert size={13} aria-hidden="true" />状态暂不可用</span>;
}

function parseContextReference(raw: string, workspaceId: string): ChatContextReference | null {
  const planningMatch = /^(?:planning|planning_request):([0-9a-f-]{36})$/i.exec(raw);
  if (planningMatch?.[1] !== undefined) {
    const id = planningMatch[1];
    return {
      key: `planning_request:${id}`,
      kind: "planning",
      resourceId: id,
      label: "规划请求",
      href: `/workspaces/${workspaceId}/planning/${encodeURIComponent(id)}`,
    };
  }
  const taskMatch = /^task:([0-9a-f-]{36})$/i.exec(raw);
  if (taskMatch?.[1] !== undefined) {
    const id = taskMatch[1];
    return {
      key: `task:${id}`,
      kind: "task",
      resourceId: id,
      label: "任务",
      href: `/workspaces/${workspaceId}/tasks/${encodeURIComponent(id)}`,
    };
  }
  const subtaskMatch = /^subtask:([0-9a-f-]{36}):([^/:]+)$/i.exec(raw);
  if (subtaskMatch?.[1] !== undefined && subtaskMatch[2] !== undefined) {
    const rootTaskId = subtaskMatch[1];
    const subtaskKey = subtaskMatch[2];
    return {
      key: `subtask:${rootTaskId}:${subtaskKey}`,
      kind: "subtask",
      resourceId: subtaskKey,
      label: "子任务",
      href: `/workspaces/${workspaceId}/tasks/${encodeURIComponent(rootTaskId)}/subtasks/${encodeURIComponent(subtaskKey)}`,
    };
  }
  const workerMatch = /^worker_item:([0-9a-f-]{36})$/i.exec(raw);
  if (workerMatch?.[1] !== undefined) {
    const id = workerMatch[1];
    return {
      key: `worker_item:${id}`,
      kind: "worker-item",
      resourceId: id,
      label: "WorkerItem",
      href: `/workspaces/${workspaceId}/worker-items/${encodeURIComponent(id)}`,
    };
  }
  return null;
}
