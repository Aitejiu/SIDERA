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

export interface MainAgentThread {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  contextRefs: string[];
  turns: MainAgentTurn[];
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
}

export interface MainAgentCapabilities {
  thread_list: true;
  thread_create: true;
  thread_read: true;
  thread_rename: false;
  turn_stream: true;
  turn_interrupt: true;
  stream_resume_after_gateway_restart: false;
}

export interface MainAgentHealth {
  status: "ok";
  service: "sidera-main-agent-gateway";
  provider: "available" | "unavailable";
}

export interface MainAgentErrorEnvelope {
  error: {
    code: string;
    message: string;
    retryable: boolean;
  };
}
