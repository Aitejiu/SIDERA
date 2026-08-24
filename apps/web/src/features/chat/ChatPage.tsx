import { CircleAlert, CircleCheck, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createThread,
  getHealth,
  getThread,
  getTurn,
  interruptTurn,
  listThreads,
  mainAgentHealthQueryKey,
  mainAgentThreadQueryKey,
  mainAgentThreadsQueryKey,
  startTurn,
  MainAgentApiProblem,
} from "./api";
import { ChatComposer } from "./ChatComposer";
import { ChatContextPanel, collectContextReferences } from "./ChatContextPanel";
import { ChatThreadList } from "./ChatThreadList";
import { ChatTranscript, getTurnInput } from "./ChatTranscript";
import { parseToolResult } from "./ToolResultCard";
import { subscribeTurnEvents } from "./turnStream";
import type { JsonValue, MainAgentItem, MainAgentTurn } from "./types";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const NEW_THREAD_SCOPE = "__new__";
const ACTIVE_STATUSES = new Set<MainAgentTurn["status"]>(["queued", "in_progress"]);
const TERMINAL_EVENT_NAMES = new Set(["completed", "failed", "interrupted", "cancelled"]);
const THREAD_BUSY_RETRY_DELAYS_MS = [250, 500, 1000, 2000, 4000, 8000] as const;

type ScopedTurnMap = Record<string, Record<string, MainAgentTurn>>;
type ScopedInputMap = Record<string, Record<string, string>>;

export function ChatPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const threadId = useParams<{ threadId?: string }>().threadId;
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const scope = threadId ?? NEW_THREAD_SCOPE;
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [transientByScope, setTransientByScope] = useState<ScopedTurnMap>({});
  const [turnInputsByScope, setTurnInputsByScope] = useState<ScopedInputMap>({});
  const [pollingTurn, setPollingTurn] = useState<{ threadId: string; turnId: string } | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [contextSendPending, setContextSendPending] = useState(false);
  const [sendPending, setSendPending] = useState(false);
  const rawContextRef = searchParams.get("context_ref");
  const pendingContextRef = useMemo(() => {
    if (rawContextRef === null) return null;
    return collectContextReferences([rawContextRef], [], workspaceId)[0]?.key ?? null;
  }, [rawContextRef, workspaceId]);
  const draft = drafts[scope] ?? (scope === NEW_THREAD_SCOPE ? pendingContextRef ?? "" : "");
  const transientTurns = useMemo(
    () => transientByScope[scope] ?? {},
    [scope, transientByScope],
  );
  const turnInputs = useMemo(
    () => turnInputsByScope[scope] ?? {},
    [scope, turnInputsByScope],
  );

  const healthQuery = useQuery({
    queryKey: mainAgentHealthQueryKey(workspaceId),
    queryFn: ({ signal }) => getHealth(signal),
    enabled: workspaceId.length > 0,
    retry: false,
  });
  const threadsQuery = useQuery({
    queryKey: mainAgentThreadsQueryKey(workspaceId),
    queryFn: ({ signal }) => listThreads(undefined, 50, signal),
    enabled: workspaceId.length > 0,
  });
  const threadQuery = useQuery({
    queryKey: mainAgentThreadQueryKey(workspaceId, threadId ?? ""),
    queryFn: ({ signal }) => getThread(threadId ?? "", signal),
    enabled: threadId !== undefined,
  });

  const createMutation = useMutation({
    mutationFn: () => createThread({ contextRefs: [], title: "新对话" }),
    onSuccess: (thread) => {
      queryClient.setQueryData(mainAgentThreadQueryKey(workspaceId, thread.id), thread);
      void queryClient.invalidateQueries({ queryKey: mainAgentThreadsQueryKey(workspaceId) });
      navigate(`/workspaces/${workspaceId}/chat/${encodeURIComponent(thread.id)}`);
    },
    onError: () => setErrorMessage("无法创建对话，请稍后重试。"),
  });

  const selectedThread = threadQuery.data;
  const turns = useMemo(
    () => mergeTurns(selectedThread?.turns ?? [], transientTurns),
    [selectedThread?.turns, transientTurns],
  );
  const activeTurn = useMemo(
    () => [...turns].reverse().find((turn) => ACTIVE_STATUSES.has(turn.status)),
    [turns],
  );
  const toolResourceRefs = useMemo(
    () => turns.flatMap((turn) => turn.items
      .map((item) => item.type === "toolCall" ? parseToolResult(item)?.resourceRef ?? null : null)),
    [turns],
  );
  const contextReferences = useMemo(
    () => collectContextReferences(
      selectedThread?.contextRefs ?? (pendingContextRef === null ? [] : [pendingContextRef]),
      toolResourceRefs,
      workspaceId,
    ),
    [pendingContextRef, selectedThread?.contextRefs, toolResourceRefs, workspaceId],
  );

  const applyTurn = useCallback((turn: MainAgentTurn) => {
    setTransientByScope((current) => ({
      ...current,
      [turn.threadId]: { ...current[turn.threadId], [turn.id]: turn },
    }));
    if (!ACTIVE_STATUSES.has(turn.status)) {
      void queryClient.invalidateQueries({
        queryKey: mainAgentThreadQueryKey(workspaceId, turn.threadId),
      });
    }
  }, [queryClient, workspaceId]);

  useEffect(() => {
    if (threadId === undefined || activeTurn === undefined) {
      return;
    }
    if (!activeTurn.streamAvailable) {
      return;
    }
    if (typeof globalThis.EventSource !== "function") {
      return;
    }
    const subscription = subscribeTurnEvents(threadId, activeTurn.id, {
      onEvent: (event) => {
        if (event.event === "item") {
          const item = readItem(event.data.item);
          if (item !== null) {
            setTransientByScope((current) => {
              const previous = current[threadId]?.[activeTurn.id] ?? activeTurn;
              const items = previous.items.some((candidate) => candidate.id === item.id)
                ? previous.items.map((candidate) => candidate.id === item.id ? item : candidate)
                : [...previous.items, item];
              return {
                ...current,
                [threadId]: { ...current[threadId], [activeTurn.id]: { ...previous, items } },
              };
            });
          }
        }
        if (TERMINAL_EVENT_NAMES.has(event.event)) {
          void refreshTurn(threadId, activeTurn.id, applyTurn);
        }
      },
      onResync: () => setPollingTurn({ threadId, turnId: activeTurn.id }),
      onPollingRequired: () => setPollingTurn({ threadId, turnId: activeTurn.id }),
    });
    return () => subscription.close();
  }, [activeTurn, applyTurn, threadId]);

  const pollingTarget = useMemo(() => {
    if (threadId === undefined) return null;
    if (pollingTurn?.threadId === threadId) return pollingTurn;
    if (activeTurn !== undefined && !activeTurn.streamAvailable) {
      return { threadId, turnId: activeTurn.id };
    }
    return null;
  }, [activeTurn, pollingTurn, threadId]);

  useEffect(() => {
    if (pollingTarget === null) {
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const turn = await getTurn(pollingTarget.threadId, pollingTarget.turnId);
        if (cancelled) return;
        applyTurn(turn);
        if (ACTIVE_STATUSES.has(turn.status)) {
          timer = window.setTimeout(() => void poll(), 1000);
        } else {
          if (pollingTurn?.threadId === pollingTarget.threadId
            && pollingTurn.turnId === pollingTarget.turnId) {
            setPollingTurn(null);
          }
        }
      } catch {
        if (!cancelled) {
          timer = window.setTimeout(() => void poll(), 1500);
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [applyTurn, pollingTarget, pollingTurn]);

  const selectThread = (nextThreadId: string) => {
    navigate(`/workspaces/${workspaceId}/chat/${encodeURIComponent(nextThreadId)}`);
  };

  const send = async (input: string) => {
    const normalizedInput = input.trim();
    if (activeTurn !== undefined || normalizedInput === "" || sendPending) return;
    setErrorMessage(null);

    if (threadId === undefined) {
      if (pendingContextRef === null || contextSendPending) return;
      setContextSendPending(true);
      try {
        const created = await createThread({
          contextRefs: [pendingContextRef],
          title: "上下文讨论",
        });
        queryClient.setQueryData(mainAgentThreadQueryKey(workspaceId, created.id), created);
        void queryClient.invalidateQueries({ queryKey: mainAgentThreadsQueryKey(workspaceId) });
        setDrafts((current) => ({ ...current, [created.id]: normalizedInput }));
        navigate(`/workspaces/${workspaceId}/chat/${encodeURIComponent(created.id)}`);
        try {
          const turn = await startTurnWithBusyRetry(created.id, normalizedInput);
          setTurnInputsByScope((current) => ({
            ...current,
            [created.id]: { ...current[created.id], [turn.id]: normalizedInput },
          }));
          applyTurn(turn);
          setDrafts((current) => ({ ...current, [created.id]: "" }));
          if (!turn.streamAvailable) {
            setPollingTurn({ threadId: created.id, turnId: turn.id });
          }
        } catch {
          setErrorMessage("消息未发送，输入内容已保留。请检查 Gateway 后重试。");
        }
      } catch {
        setErrorMessage("无法创建带上下文的对话，输入内容已保留。请稍后重试。");
      } finally {
        setContextSendPending(false);
      }
      return;
    }

    setSendPending(true);
    try {
      const turn = await startTurnWithBusyRetry(threadId, normalizedInput);
      setTurnInputsByScope((current) => ({
        ...current,
        [threadId]: { ...current[threadId], [turn.id]: normalizedInput },
      }));
      applyTurn(turn);
      setDrafts((current) => ({ ...current, [scope]: "" }));
      if (!turn.streamAvailable) {
        setPollingTurn({ threadId, turnId: turn.id });
      }
    } catch {
      setErrorMessage("消息未发送，输入内容已保留。请检查 Gateway 后重试。");
    } finally {
      setSendPending(false);
    }
  };

  const retry = async (turnId: string) => {
    if (threadId === undefined || activeTurn !== undefined) return;
    const sourceTurn = turns.find((turn) => turn.id === turnId);
    const input = turnInputs[turnId] ?? (sourceTurn === undefined ? null : getTurnInput(sourceTurn));
    if (input === null || input === undefined) {
      setErrorMessage("这轮对话没有可重试的原始输入。");
      return;
    }
    await send(input);
  };

  const interrupt = async () => {
    if (threadId === undefined || activeTurn === undefined) return;
    setErrorMessage(null);
    try {
      const turn = await interruptTurn(threadId, activeTurn.id);
      applyTurn(turn);
      setPollingTurn(null);
    } catch {
      setErrorMessage("停止请求未完成，请刷新当前对话确认状态。");
    }
  };

  const unavailable = healthQuery.isError || healthQuery.data?.provider === "unavailable";
  const pageTitle = selectedThread?.title ?? (pendingContextRef === null ? "Main Agent" : "上下文讨论");

  return (
    <section className="chat-page" aria-labelledby="chat-page-title">
      <div className="breadcrumb">工作区 / 聊天</div>
      <div className="chat-page__heading">
        <div>
          <span className="eyebrow">MAIN AGENT / CHAT</span>
          <h1 id="chat-page-title">{pageTitle}</h1>
        </div>
        <div className="chat-page__status">
          {unavailable ? <CircleAlert size={15} aria-hidden="true" /> : <CircleCheck size={15} aria-hidden="true" />}
          <span>{unavailable ? "Gateway offline" : "Gateway ready"}</span>
        </div>
      </div>

      <div className="chat-workspace">
        <ChatThreadList
          threads={threadsQuery.data?.data ?? []}
          activeThreadId={threadId}
          isLoading={threadsQuery.isPending}
          isError={threadsQuery.isError}
          onSelect={selectThread}
          onCreate={() => createMutation.mutate()}
          onRetry={() => void threadsQuery.refetch()}
        />

        <main className="chat-main" aria-label="Main Agent 对话工作面">
          {threadId !== undefined && activeTurn !== undefined && (!activeTurn.streamAvailable || pollingTurn !== null) ? (
            <div className="chat-recovery-notice" role="status" aria-label="对话恢复状态">
              <RefreshCw size={16} aria-hidden="true" />
              <div>
                <strong>正在恢复原回合状态</strong>
                <span>不会自动重发这条消息；Gateway 重启后可能缺少部分实时片段。</span>
              </div>
            </div>
          ) : null}
          {unavailable ? (
            <div className="chat-main__offline" role="alert">
              <CircleAlert size={19} aria-hidden="true" />
              <strong>Main Agent 暂不可用</strong>
              <span>任务控制面仍可继续查看。</span>
            </div>
          ) : threadId === undefined && pendingContextRef !== null ? (
            <div className="chat-main__draft">
              <div className="chat-main__draft-card">
                <span className="eyebrow">CONTEXT DRAFT</span>
                <h2>围绕这个对象开始讨论</h2>
                <code>{pendingContextRef}</code>
              </div>
              <ChatComposer
                value={draft}
                active={false}
                disabled={contextSendPending}
                errorMessage={errorMessage}
                onChange={(value) => {
                  setDrafts((current) => ({ ...current, [scope]: value }));
                  setErrorMessage(null);
                }}
                onSend={() => void send(draft)}
                onInterrupt={() => void interrupt()}
              />
            </div>
          ) : threadId === undefined ? (
            <div className="chat-main__empty">
              <span className="eyebrow">NO THREAD SELECTED</span>
              <h2>选择一个对话</h2>
              <button className="quiet-button" type="button" onClick={() => createMutation.mutate()}>
                新建对话
              </button>
            </div>
          ) : threadQuery.isPending ? (
            <div className="chat-main__state">正在读取对话内容</div>
          ) : threadQuery.isError ? (
            <div className="chat-main__state chat-main__state--error" role="alert">
              <strong>无法读取这段对话</strong>
              <button className="quiet-button" type="button" onClick={() => void threadQuery.refetch()}>
                <RefreshCw size={14} aria-hidden="true" />重试读取
              </button>
            </div>
          ) : (
            <>
              <ChatTranscript
                turns={turns}
                workspaceId={workspaceId}
                onRetry={(turnId) => void retry(turnId)}
              />
              <ChatComposer
                value={draft}
                active={activeTurn !== undefined}
                disabled={threadQuery.isError || createMutation.isPending || contextSendPending || sendPending}
                errorMessage={errorMessage}
                onChange={(value) => {
                  setDrafts((current) => ({ ...current, [scope]: value }));
                  setErrorMessage(null);
                }}
                onSend={() => void send(draft)}
                onInterrupt={() => void interrupt()}
              />
            </>
          )}
        </main>

        <ChatContextPanel
          workspaceId={workspaceId}
          references={contextReferences}
          taskLinkLabel={unavailable ? "返回任务" : "打开任务控制面"}
        />
      </div>
    </section>
  );
}

async function startTurnWithBusyRetry(threadId: string, input: string): Promise<MainAgentTurn> {
  for (let retryIndex = 0; ; retryIndex += 1) {
    try {
      return await startTurn(threadId, input);
    } catch (error) {
      const retryDelay = THREAD_BUSY_RETRY_DELAYS_MS[retryIndex];
      if (!isRetryableThreadBusy(error) || retryDelay === undefined) {
        throw error;
      }
      await wait(retryDelay);
    }
  }
}

function isRetryableThreadBusy(error: unknown): error is MainAgentApiProblem {
  return error instanceof MainAgentApiProblem
    && error.status === 409
    && error.code === "thread_busy"
    && error.retryable;
}

function wait(delayMs: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, delayMs);
  });
}

async function refreshTurn(
  threadId: string,
  turnId: string,
  applyTurn: (turn: MainAgentTurn) => void,
): Promise<void> {
  try {
    applyTurn(await getTurn(threadId, turnId));
  } catch {
    // The next query refresh or the user retry owns recovery after a terminal stream error.
  }
}

function mergeTurns(
  persisted: MainAgentTurn[],
  transient: Record<string, MainAgentTurn>,
): MainAgentTurn[] {
  const merged = new Map(persisted.map((turn) => [turn.id, turn]));
  for (const turn of Object.values(transient)) {
    merged.set(turn.id, turn);
  }
  return [...merged.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt));
}

function readItem(value: JsonValue | undefined): MainAgentItem | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== "string" || typeof value.type !== "string" || !isRecord(value.data)) return null;
  if (!isItemType(value.type)) return null;
  return { id: value.id, type: value.type, data: value.data as Record<string, JsonValue> };
}

function isItemType(value: string): value is MainAgentItem["type"] {
  return value === "userMessage" || value === "assistantMessage" || value === "toolCall" || value === "error";
}

function isRecord(value: JsonValue | undefined): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
