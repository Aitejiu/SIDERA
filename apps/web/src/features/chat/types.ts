import type { components } from "@/shared/api/product-schema";

export type ThreadStop = components["schemas"]["ThreadStopView"];

export type TurnStatus =
  | "queued"
  | "in_progress"
  | "completed"
  | "interrupted"
  | "failed"
  | "cancelled";

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface ChatProfileRef {
  workspace_id: string;
  profile_id: string;
  revision: number;
}

export interface MainAgentThread {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  contextRefs: string[];
  turns: MainAgentTurn[];
  profileRef?: ChatProfileRef;
  runId?: string | null;
}

export interface MainAgentThreadList {
  data: MainAgentThread[];
  nextCursor: string | null;
}

export interface MainAgentItem {
  id: string;
  type: "userMessage" | "assistantMessage" | "toolCall" | "error";
  data: Record<string, JsonValue>;
}

export interface MainAgentTurn {
  id: string;
  threadId: string;
  status: TurnStatus;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
  finalResponse: string | null;
  items: MainAgentItem[];
  usage: Record<string, JsonValue> | null;
  error: Record<string, JsonValue> | null;
  streamAvailable: boolean;
  recoveryMode: "poll" | null;
  profileRef?: ChatProfileRef;
  runId?: string | null;
  responseId?: string | null;
  operationId?: string;
  operationState?: "claimed" | "prepared" | "dispatching" | "accepted" | "delivered" | "busy" | "rejected" | "unknown";
  interruptAvailable?: boolean;
  interruptReasonCode?: string | null;
}

export interface MainAgentCapabilities {
  thread_list: boolean;
  thread_create: boolean;
  thread_read: boolean;
  thread_rename: boolean;
  turn_stream: boolean;
  turn_interrupt: boolean;
  stream_resume_after_gateway_restart: boolean;
}

export interface MainAgentHealth {
  status: "ok";
  service: "sidera-main-agent-gateway" | "sidera-product";
  provider: "available" | "unavailable";
}

export interface MainAgentErrorEnvelope {
  error: {
    code: string;
    message: string;
    retryable: boolean;
  };
}
