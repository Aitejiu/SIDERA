# 集成与 API

此入口面向需要接入 SIDERA 的 Agent、工具和界面开发者。只想使用产品，请先阅读
[快速开始](../deployment/initial-demo.md)和[使用指南](../guide/workspace.md)。

以下文档描述当前实现，不是未来能力承诺。API 仍处于早期预览，接入方应固定 SIDERA
版本，并在升级时核对 OpenAPI；不要直接读写 SIDERA 数据库。

## 服务入口

按快速开始启动后，可以打开服务自带的交互文档：

| 服务 | 本地文档 | 契约或配置 |
| --- | --- | --- |
| Control Plane | <http://127.0.0.1:18625/docs> | [OpenAPI JSON](../../apps/web/openapi.json) |
| Knowledge Service | <http://127.0.0.1:18627/docs> | [OpenAPI JSON](../../apps/web/knowledge-openapi.json) |
| Main Agent Gateway | 单独启动后访问该服务的 `/docs` | [配置与启动](../../services/main-agent-gateway/README.md) |

每个服务也提供 `/openapi.json`。实际请求字段、响应类型和分页参数以正在运行的版本为准。
控制面路径以 `/api/v1` 开头，聊天网关路径以 `/main-agent/v1` 开头。
Knowledge 与控制面都使用 `/api/v1`，但属于不同服务，不要把它们发到同一地址。

当前 Control Plane 使用本地单人身份，不提供生产级认证。请求中的 `actor` 字段不是
身份认证凭据；部分命令还会校验配置的本地 actor。服务必须保持在 loopback，不能把
浏览器 CORS 或 workspace UUID 当成安全隔离。

## 从只读接入开始

先启动无模型 Demo，通过任务摘要逐层获取对象 ID，再查询具体工作空间：

```bash
curl --fail \
  http://127.0.0.1:18625/api/v1/workspaces/20000000-0000-0000-0000-000000000001/task-overviews
```

| 用途 | HTTP 接口 |
| --- | --- |
| 任务列表 | `GET /api/v1/workspaces/{workspace_id}/task-overviews` |
| 根任务总览 | `GET /api/v1/tasks/{root_task_id}/overview` |
| 依赖图 | `GET /api/v1/tasks/{root_task_id}/graph` |
| 子任务 | `GET /api/v1/tasks/{root_task_id}/subtasks/{subtask_key}` |
| WorkerItem 工作空间 | `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace` |
| WorkerItem 工作记录 | `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity` |
| Trace | `GET /api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace` |
| 信箱 | `GET /api/v1/workspaces/{workspace_id}/notification-threads` |
| 命令状态 | `GET /api/v1/workspaces/{workspace_id}/commands/{command_id}` |
| 工作空间能力 | `GET /api/v1/workspaces/{workspace_id}/capabilities` |

上述 `/tasks/...` 查询都要求额外的 `workspace_id` query 参数。
工作记录使用 `after`/`limit`，Trace 使用 `before_sequence`/`limit`，信箱使用 `cursor`/`limit`。
各接口分页格式不同，不要共用一个猜测的游标规则。

WorkerItem 响应提供 `can_intervene`、`read_only_reason`、`intervention_context` 和
`trace_descriptor`。据此决定是否展示操作及当前 Trace 地址，不要根据 Worker 名称
或某条日志推测当前会话。缺失的用量或 Trace 保持未知，不补成零或成功。

## 提交操作

用户操作通过 `POST /api/v1/commands` 提交。公共包络含命令 ID、工作空间、actor、
目标对象、预期版本、幂等键和具体 payload；命令名不是任意自然语言。

[命令与介入示例](commands.md)给出完整包络、真实的 `SendExecutionInput` payload
及失败恢复规则。OpenAPI 中的 `payload` 是通用对象，并不单独列出所有业务命令字段；
新增命令接入时还需核对对应实现和测试，不能从包络 schema 推断字段。

收到写入成功，只代表该命令已应用到控制面；实际 Agent 执行、消息投递和其他外部动作
仍需相应进程处理。Demo 默认不运行这些进程。

## 主 Agent 与聊天

已有的 [Akashic Task Bridge](../../integrations/akashic/README.md)可作为主 Agent 接入参考，
其中列出插件配置、工具调用和安装步骤。Web 聊天通过
[Main Agent Gateway](../../services/main-agent-gateway/README.md)访问 thread/turn 接口，
模型凭据留在服务端，不交给浏览器。

接入另一个主 Agent 时，通过控制面 API 提交请求、读取提案和任务状态；
确认操作必须保留用户确认对象的准确版本。不要把聊天成功等同于任务已执行。

## Knowledge Service

知识接口使用 `Authorization: Bearer <本地配置的 capability token>`，能力分为
`local_owner`、`main_agent` 和 `control_plane`。缺失能力配置返回不可用，无权访问会被拒绝；
不能为了便利向每个调用方发放 owner token。

页面、修订、检索、候选审核和命令接口见独立 OpenAPI 与
[Knowledge Service 说明](../../services/knowledge-service/README.md)。正式凭据仅放在
受保护的本地配置中，示例 token 不得用于共享环境。

## 组件开发

- [Control Plane](../../services/control-plane/README.md)：数据库准备、运行模块、命令示例和测试。
- [Web](../../apps/web/README.md)：前端运行、API 类型与浏览器测试。
- [Akashic Task Bridge](../../integrations/akashic/README.md)：现有主 Agent 适配器。

报告接入问题时提供 SIDERA 提交版本、脱敏请求、HTTP 状态码和错误码即可。
不要提交模型 key、知识正文或完整私人会话。
