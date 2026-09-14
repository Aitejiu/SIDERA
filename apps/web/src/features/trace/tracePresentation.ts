import { Bot, CheckCheck, CircleHelp, FileArchive, MessageSquare, Network, OctagonAlert, Wrench } from "lucide-react";

const categories = {
  input: { label: "输入", icon: MessageSquare, tone: "input" },
  assistant_summary: { label: "模型输出", icon: Bot, tone: "model" },
  model: { label: "模型", icon: Bot, tone: "model" },
  tool: { label: "工具", icon: Wrench, tone: "tool" },
  subtool: { label: "子工具", icon: Wrench, tone: "tool" },
  checkpoint: { label: "检查点", icon: CheckCheck, tone: "checkpoint" },
  artifact: { label: "产物", icon: FileArchive, tone: "checkpoint" },
  scheduler: { label: "调度", icon: Network, tone: "other" },
  error: { label: "异常", icon: OctagonAlert, tone: "error" },
};

export function traceCategory(category: string) {
  return Object.hasOwn(categories, category) ? categories[category as keyof typeof categories]
    : { label: category || "其他", icon: CircleHelp, tone: "other" };
}

export function traceStatus(status: string) {
  const labels: Record<string, string> = {
    running: "进行中", active: "进行中", completed: "已完成", succeeded: "已完成",
    failed: "失败", error: "异常", canceled: "已取消", cancelled: "已取消",
    pending: "等待中", accepted: "已接收", assigned: "已分配", unknown: "未知",
    connecting: "读取中", blocked: "受阻", paused: "已暂停", waiting: "等待中",
  };
  return Object.hasOwn(labels, status) ? labels[status]! : status;
}

export function traceIsError(status: string, category: string) {
  return category === "error" || status === "failed" || status === "error";
}

export function formatTraceDuration(value: number | null) {
  if (value === null) return "未提供";
  return value < 1_000 ? `${value} ms` : `${(value / 1_000).toFixed(1)} s`;
}
