# 命令与介入示例

[返回 API 导航](README.md)

以下以局部介入为例说明当前控制面命令合同。此操作会写入当前工作空间，
只在你有权操作的环境运行。新版要求本地会话、精确 Origin 和当前 `X-Sidera-CSRF`；
actor 不是授权。模拟 Demo 可以保存记录，但不执行真实模型请求。

## 公共包络

向 `POST /api/v1/commands` 发送 JSON。所有下列字段都必须出现；允许为 `null` 的字段
仍须遵循该命令的业务约束，不是所有命令都接受空目标或空版本。

| 字段 | 用法 |
| --- | --- |
| `command_id` | 本次命令的 UUID；不确定结果时用于查询 |
| `workspace_id` | 目标工作空间 UUID |
| `command_type` | 已实现的命令名 |
| `target_type`、`target_id` | 命令要求的对象类型和 UUID |
| `actor` | 含 `type`、`id` 的本地调用者信息，不是认证令牌 |
| `expected_version` | 本次确认所依据的目标版本；仅允许时可为 `null` |
| `idempotency_key` | 一次逻辑操作的稳定标识 |
| `correlation_id` | 用于关联同一操作流程的 UUID |
| `payload` | 该命令的业务参数 |

成功响应包含 `command_id`、`aggregate_id`、`aggregate_version`、`event_ids`，
以及命令需要的 `data`。不要把 HTTP 成功解释为外部 Worker 已完成动作。

## Native 局部介入

命令仍为 `SendExecutionInput`，目标是当前 `execution_session`，不是 WorkerItem ID。
先读取 `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/native-context`，
核对可介入状态、绑定、Profile、Execution 和 Session，让用户明确查看最新上下文。
将响应中的完整 `context` 原样放进 `payload.context`。

Native context 在下方 legacy 七字段之外包含 `execution_mode: "native_profile"`、
`binding_version`、`run_id`。若返回 `session_last_sequence`，保留实际值；它是非负整数
且小于 `session_version`，不能猜测。`target_id`、`expected_version` 分别使用 context 的
`session_id`、`session_version`。使用当前用户身份、新逻辑操作 ID、正文及 `attachment_refs`，
不要手工生成精简上下文。实现见 [native 介入状态](../../apps/web/src/features/trace/useNativeIntervention.ts)。

只有活动且已接入的 Session 可接收输入，关闭后不会自动创建续作 Session。
受理、投递、执行分别观察，命令 applied 不等于 Agent 已处理。

## Legacy 局部介入示例

先读取 `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace`。
仅在 `can_intervene` 为真且 `intervention_context` 非空时准备输入。

当前实现的命令名是 `SendExecutionInput`，不是 `SendWorkerInput`。
产品上的入口是 WorkerItem，HTTP 命令的目标却是它当前的 `execution_session`；
这样能够校验消息是否仍然发给用户查看时的那一次执行。

以下 JSON 仅适用于 legacy，不适用于 native Profile 执行。
所有 UUID、版本和 actor 都是占位示例，不能直接当作线上对象使用：

```json
{
  "command_id": "00000000-0000-4000-8000-000000000001",
  "workspace_id": "00000000-0000-4000-8000-000000000002",
  "command_type": "SendExecutionInput",
  "target_type": "execution_session",
  "target_id": "00000000-0000-4000-8000-000000000003",
  "actor": { "type": "local", "id": "configured-local-actor" },
  "expected_version": 1,
  "idempotency_key": "worker-input-example-001",
  "correlation_id": "00000000-0000-4000-8000-000000000004",
  "payload": {
    "context": {
      "worker_item_id": "00000000-0000-4000-8000-000000000005",
      "plan_generation": 1,
      "context_version": 1,
      "execution_id": "00000000-0000-4000-8000-000000000006",
      "session_id": "00000000-0000-4000-8000-000000000003",
      "session_version": 1,
      "worker_id": "00000000-0000-4000-8000-000000000007"
    },
    "client_idempotency_key": "worker-input-example-001",
    "text": "Please verify the callback deduplication behavior before continuing.",
    "attachment_refs": []
  }
}
```

实际发送时：

1. 将查询返回的 `intervention_context` 原样放入 `payload.context`。
2. `target_id` 使用其中的 `session_id`，`expected_version` 使用 `session_version`。
3. 使用配置的本地 actor，为新的逻辑操作生成新的命令 UUID 和幂等键。
4. `text` 为输入内容，`attachment_refs` 无附件时仍传空数组。
5. 同一次输入重试保持原包络、正文与 `client_idempotency_key`，不要用旧键发送修改后的内容。

会话中已有待处理输入时，新的发送可能被拒绝。不要不断重发；先读取原输入状态，
必要时使用已有的修改或取消待处理输入操作。Web 调用示例见
[介入 API 客户端](../../apps/web/src/features/trace/api.ts)。

## 处理冲突与未知结果

- `404`：对象不存在或不属于当前 workspace；不要切换到其他 workspace 猜测重试。
- `409`：目标版本或执行上下文发生变化。保留草稿，重新查询，并让用户确认后再建立新操作。
- `422`：请求或业务参数不满足约束，按错误码检查字段；不要原样持续重试。
- 网络超时或服务异常：先查询 `GET /api/v1/workspaces/{workspace_id}/commands/{command_id}`。
  `pending`、`applied`、`rejected` 分别表示命令仍在处理、已应用或已拒绝。

查询不到命令不能证明外部动作一定没有发生。恢复时保留原命令身份和幂等键，
不要另造命令重复发送，也不要自动把旧输入改绑到新的 Session。
已保存输入的后续投递状态仍需从执行输入或工作记录查询，不由命令状态代替。

## Profile 交接

使用现有 `PrepareProfileHandoff`、`ConfirmProfileHandoff`，先准备并查看精确目标与
上下文，再确认原提案。字段以运行版本 OpenAPI 和
[交接客户端](../../apps/web/src/features/assignments/ProfileHandoffDialog.tsx)为准，不另造命令名。
原 Agent 停止必须得到确认；目标接入与上下文 ACK 分开观察。WorkerItem 历史保留，
局部交接不更换整个计划代。UNKNOWN 查询原命令/transition，不重新确认或自动换绑。
