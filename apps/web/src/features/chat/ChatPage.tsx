import { CircleAlert, CircleCheck, RefreshCw } from "lucide-react";
import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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
type PendingOperationState = "unknown" | "conflict" | "busy";
type PendingOperation = { operationId: string; state: PendingOperationState };
type ScopedOperationMap = Record<string, PendingOperation>;

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
  const [pendingOperationsByScope, setPendingOperationsByScope] = useState<ScopedOperationMap>({});
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
  const pendingOperation = pendingOperationsByScope[scope];

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
  const activeTurnId = activeTurn?.id;
  const streamAvailable = activeTurn?.streamAvailable ?? false;
  const activeTurnRef = useRef<MainAgentTurn | undefined>(undefined);
  useEffect(() => { activeTurnRef.current = activeTurn; }, [activeTurn]);
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
    if (turn.operationState === "accepted" || turn.operationState === "delivered") {
      setPendingOperationsByScope((current) => {
        if (current[turn.threadId]?.operationId !== turn.operationId) return current;
        const next = { ...current };
        delete next[turn.threadId];
        return next;
      });
    }
    if (!ACTIVE_STATUSES.has(turn.status)) {
      void queryClient.invalidateQueries({
        queryKey: mainAgentThreadQueryKey(workspaceId, turn.threadId),
      });
    }
  }, [queryClient, workspaceId]);

  useEffect(() => {
    if (threadId === undefined || activeTurnId === undefined) return;
    const controller = new AbortController();
    let stopped = false;
    let inFlight = false;
    let timer: number | undefined;
    const subscription: { current?: ReturnType<typeof subscribeTurnEvents> } = {};
    const stop = () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller.abort();
      subscription.current?.close();
    };
    const schedule = () => {
      if (stopped) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => { timer = undefined; void refresh(); }, 2000);
    };
    const refresh = async () => {
      if (stopped || inFlight) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = undefined;
      inFlight = true;
      try {
        const turn = await getTurn(threadId, activeTurnId, controller.signal);
        if (stopped) return;
        applyTurn(turn);
        if (!ACTIVE_STATUSES.has(turn.status)) {
          setPollingTurn(null);
          stop();
        }
      } catch (error) {
        if (stopped) return;
        if (error instanceof MainAgentApiProblem && (error.status === 401 || error.status === 403)) {
          setErrorMessage("本地会话不可用，请重新授权。");
          setPollingTurn(null);
          stop();
        }
      } finally {
        inFlight = false;
        if (!stopped) schedule();
      }
    };
    const fallback = (status = 409) => {
      if (stopped) return;
      if (status === 401 || status === 403) {
        setErrorMessage("本地会话不可用，请重新授权。");
        stop();
        return;
      }
      setPollingTurn({ threadId, turnId: activeTurnId });
      void refresh();
    };
    if (!streamAvailable || typeof globalThis.EventSource !== "function") {
      void refresh();
      return stop;
    }
    subscription.current = subscribeTurnEvents(threadId, activeTurnId, {
      onEvent: (event) => {
        if (stopped) return;
        if (event.event === "item") {
          const item = readItem(event.data.item);
          if (item !== null) {
            setTransientByScope((current) => {
              const previous = current[threadId]?.[activeTurnId] ?? activeTurnRef.current;
              if (previous === undefined || previous.id !== activeTurnId || previous.threadId !== threadId) return current;
              const items = previous.items.some((candidate) => candidate.id === item.id)
                ? previous.items.map((candidate) => candidate.id === item.id ? item : candidate)
                : [...previous.items, item];
              return {
                ...current,
                [threadId]: { ...current[threadId], [activeTurnId]: { ...previous, items } },
              };
            });
            if (item.type === "toolCall") void refresh();
          }
        }
        if (TERMINAL_EVENT_NAMES.has(event.event)) {
          void refresh();
        }
      },
      onResync: () => fallback(),
      onPollingRequired: fallback,
    });
    if (stopped) subscription.current.close();
    else schedule();
    return stop;
  }, [activeTurnId, applyTurn, streamAvailable, threadId]);

  const selectThread = (nextThreadId: string) => {
    navigate(`/workspaces/${workspaceId}/chat/${encodeURIComponent(nextThreadId)}`);
  };

  const send = async (input: string, options: { freshOperation?: boolean; operationId?: string } = {}) => {
    const normalizedInput = options.operationId === undefined ? input.trim() : input;
    if (activeTurn !== undefined || normalizedInput.trim() === "" || sendPending) return;
    setErrorMessage(null);
    const operationId = options.operationId ?? (options.freshOperation
      ? createOperationId()
      : pendingOperation?.operationId ?? createOperationId());

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
          const turn = await startTurnWithBusyRetry(
            created.id,
            normalizedInput,
            operationId,
          );
          setTurnInputsByScope((current) => ({
            ...current,
            [created.id]: { ...current[created.id], [turn.id]: normalizedInput },
          }));
          applyTurn(turn);
          if (turn.operationState === "busy" || isUncertainTurn(turn)) {
            setPendingOperationsByScope((current) => ({ ...current, [created.id]: { operationId: turn.operationId ?? operationId, state: turn.operationState === "busy" ? "busy" : "unknown" } }));
          } else {
            clearPendingOperation(created.id, setPendingOperationsByScope);
            setDrafts((current) => ({ ...current, [created.id]: "" }));
          }
          if (ACTIVE_STATUSES.has(turn.status) && !turn.streamAvailable) {
            setPollingTurn({ threadId: created.id, turnId: turn.id });
          }
        } catch (error) {
          rememberPendingOperation(
            created.id,
            operationId,
            error,
            setPendingOperationsByScope,
          );
          setErrorMessage(startErrorMessage(error));
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
      const turn = await startTurnWithBusyRetry(
        threadId,
        normalizedInput,
        operationId,
      );
      setTurnInputsByScope((current) => ({
        ...current,
        [threadId]: { ...current[threadId], [turn.id]: normalizedInput },
      }));
      applyTurn(turn);
      if (turn.operationState === "busy" || isUncertainTurn(turn)) {
        setPendingOperationsByScope((current) => ({ ...current, [threadId]: { operationId: turn.operationId ?? operationId, state: turn.operationState === "busy" ? "busy" : "unknown" } }));
      } else {
        clearPendingOperation(threadId, setPendingOperationsByScope);
        setDrafts((current) => ({ ...current, [scope]: "" }));
      }
      if (ACTIVE_STATUSES.has(turn.status) && !turn.streamAvailable) {
        setPollingTurn({ threadId, turnId: turn.id });
      }
    } catch (error) {
      rememberPendingOperation(
        threadId,
        operationId,
        error,
        setPendingOperationsByScope,
      );
      setErrorMessage(startErrorMessage(error));
    } finally {
      setSendPending(false);
    }
  };

  const retry = async (turnId: string) => {
    if (threadId === undefined || activeTurn !== undefined) return;
    const sourceTurn = turns.find((turn) => turn.id === turnId);
    if (sourceTurn === undefined || isUncertainTurn(sourceTurn)) return;
    const input = turnInputs[turnId] ?? (sourceTurn === undefined ? null : getTurnInput(sourceTurn));
    if (input === null || input === undefined) {
      setErrorMessage("这轮对话没有可重试的原始输入。");
      return;
    }
    if (sourceTurn.operationState === "busy") {
      if (sourceTurn.operationId === undefined) return;
      await send(input, { operationId: sourceTurn.operationId });
    } else {
      await send(input, { freshOperation: true });
    }
  };

  const interrupt = async () => {
    if (threadId === undefined || activeTurn === undefined || activeTurn.interruptAvailable !== true) return;
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
          {pendingOperation !== undefined ? (
            <div className="chat-recovery-notice" role="status" aria-label="发送结果恢复状态">
              <RefreshCw size={16} aria-hidden="true" />
              <div>
                <strong>{pendingOperation.state === "busy" ? "请求未受理" : pendingOperation.state === "conflict" ? "原发送仍待确认" : "结果待确认"}</strong>
                <span>
                  {pendingOperation.state === "busy"
                    ? "原始输入已保留。"
                    : pendingOperation.state === "conflict"
                    ? "修改后的内容没有自动创建新回合；先结束这次恢复，再发起新的发送。"
                    : "未自动重发；再次发送会复用原 operationId，避免重复执行。"}
                </span>
              </div>
              <button
                className="quiet-button"
                type="button"
                onClick={() => {
                  clearPendingOperation(scope, setPendingOperationsByScope);
                  setErrorMessage(null);
                }}
              >
                开始新的发送
              </button>
            </div>
          ) : null}
          {threadId !== undefined && activeTurn !== undefined && (!activeTurn.streamAvailable || pollingTurn !== null) ? (
            <div className="chat-recovery-notice" role="status" aria-label="对话恢复状态">
              <RefreshCw size={16} aria-hidden="true" />
              <div>
                <strong>正在恢复原回合状态</strong>
                <span>不会自动重发这条消息；Gateway 重启后可能缺少部分实时片段。</span>
              </div>
            </div>
          ) : null}
          {unavailable && threadId !== undefined ? (
            <div className="chat-recovery-notice" role="status" aria-label="历史只读提示">
              <CircleAlert size={16} aria-hidden="true" />
              <div>
                <strong>当前显示已恢复历史</strong>
                <span>Gateway 暂不可用；维护模式下不能发送或中断新的回合。</span>
              </div>
            </div>
          ) : null}
          {unavailable && threadId === undefined ? (
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
                interruptAvailable={false}
                interruptReasonCode={null}
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
                interruptAvailable={activeTurn?.interruptAvailable === true}
                interruptReasonCode={activeTurn?.interruptReasonCode ?? "cancel_unconfirmed"}
                disabled={unavailable || threadQuery.isError || createMutation.isPending || contextSendPending || sendPending}
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

async function startTurnWithBusyRetry(
  threadId: string,
  input: string,
  operationId: string,
): Promise<MainAgentTurn> {
  for (let retryIndex = 0; ; retryIndex += 1) {
    try {
      return await startTurn(threadId, input, operationId);
    } catch (error) {
      const retryDelay = THREAD_BUSY_RETRY_DELAYS_MS[retryIndex];
      if (!isRetryableThreadBusy(error) || retryDelay === undefined) {
        throw error;
      }
      await wait(retryDelay);
    }
  }
}

function createOperationId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return "sidera-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

function clearPendingOperation(
  scope: string,
  setPendingOperations: Dispatch<SetStateAction<ScopedOperationMap>>,
): void {
  setPendingOperations((current) => {
    if (!(scope in current)) return current;
    const next = { ...current };
    delete next[scope];
    return next;
  });
}

function rememberPendingOperation(
  scope: string,
  operationId: string,
  error: unknown,
  setPendingOperations: Dispatch<SetStateAction<ScopedOperationMap>>,
): void {
  const state = isOperationIdentityConflict(error) ? "conflict" : "unknown";
  if (state === "unknown" && !isUnknownStartFailure(error)) return;
  setPendingOperations((current) => ({ ...current, [scope]: { operationId, state } }));
}

function isUnknownStartFailure(error: unknown): boolean {
  return !(error instanceof MainAgentApiProblem) || error.status >= 500;
}

function isOperationIdentityConflict(error: unknown): error is MainAgentApiProblem {
  return error instanceof MainAgentApiProblem && error.code === "operation_identity_conflict";
}

function startErrorMessage(error: unknown): string {
  if (isOperationIdentityConflict(error)) {
    return "原发送结果仍待确认，修改后的内容未发送。请先结束这次恢复，再发起新的发送。";
  }
  if (isUnknownStartFailure(error)) {
    return "发送结果未知，未自动重发。输入内容已保留，再次发送会复用原操作。";
  }
  return "消息未发送，输入内容已保留。请检查 Gateway 后重试。";
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

function isUncertainTurn(turn: MainAgentTurn): boolean {
  return turn.operationState === "claimed" || turn.operationState === "prepared"
    || turn.operationState === "dispatching" || turn.operationState === "unknown";
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
