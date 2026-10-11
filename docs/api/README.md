# 集成与 API

此入口面向需要接入 SIDERA 的 Agent、工具和界面开发者。只想使用产品，请先阅读
[快速开始](../deployment/initial-demo.md)和[使用指南](../guide/workspace.md)。

以下文档描述当前实现，不是未来能力承诺。API 仍处于早期预览，接入方应固定 SIDERA
版本，并在升级时核对 OpenAPI；不要直接读写 SIDERA 数据库。

## 新版原生产品入口

按[新版安装说明](../deployment/initial-demo.md#安装新版)启动后，访问
<http://127.0.0.1:18080/docs> 和 <http://127.0.0.1:18080/openapi.json>。
同一个产品服务提供 Control Plane `/api/v1`、智能体 `/agents/v1`、聊天
`/main-agent/v1` 及产品 `/product/v1`。Knowledge 仍是独立服务，其同名 `/api/v1`
不能发到产品服务器。旧静态 OpenAPI 不保证包含全部新版 API，以运行版本为准。

先用 CLI 的一次性 bootstrap 建立本地会话。会话 cookie 为 HttpOnly、SameSite=Strict；
cookie 身份的写请求还要求精确 Origin 和当前 `X-Sidera-CSRF`。
actor、workspace UUID、loopback 和 CORS 都不是认证凭据，不能直接写数据库。
会话操作见[部署指南](../deployment/initial-demo.md#启动与连接网页)。

可复用 Python 能力见 [Agents](../../packages/agents/README.md)、
[Control Plane](../../services/control-plane/README.md#native-public-composition) 和
[Product Server](../../apps/server/README.md)。新 Harness 实现可信的 Agents Adapter，
不应修改控制面内部状态机。当前只内置 OpenCode，没有动态插件安装或第三方代码沙箱。

## Legacy 独立服务入口

以下为保留的旧服务/模拟 Demo 入口，不是新版必需的部署拓扑：

| 服务 | 本地文档 | 契约或配置 |
| --- | --- | --- |
| Control Plane | <http://127.0.0.1:18625/docs> | [OpenAPI JSON](../../apps/web/openapi.json) |
| Knowledge Service | <http://127.0.0.1:18627/docs> | [OpenAPI JSON](../../apps/web/knowledge-openapi.json) |
| Main Agent Gateway | 单独启动后访问该服务的 `/docs` | [配置与启动](../../services/main-agent-gateway/README.md) |

每个服务也提供 `/openapi.json`。实际请求字段、响应类型和分页参数以正在运行的版本为准。
控制面路径以 `/api/v1` 开头，聊天网关路径以 `/main-agent/v1` 开头。
Knowledge 与控制面都使用 `/api/v1`，但属于不同服务，不要把它们发到同一地址。

旧 standalone Control Plane 使用本地单人身份，不提供生产级认证。请求中的 `actor` 字段不是
身份认证凭据；部分命令还会校验配置的本地 actor。服务必须保持在 loopback，不能把
浏览器 CORS 或 workspace UUID 当成安全隔离。

## 从只读接入开始

新版通过已认证的本地会话逐层读取对象。以下 curl 仅用于 legacy 无模型 Demo，
不包含新版所需认证：

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
| Legacy Trace | `GET /api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace` |
| Native 上下文 | `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/native-context` |
| Native Trace | `GET /api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/executions/{execution_id}/native-trace` |
| 信箱 | `GET /api/v1/workspaces/{workspace_id}/notification-threads` |
| 命令状态 | `GET /api/v1/workspaces/{workspace_id}/commands/{command_id}` |
| 工作空间能力 | `GET /api/v1/workspaces/{workspace_id}/capabilities` |

上述 `/tasks/...` 查询都要求额外的 `workspace_id` query 参数。
工作记录使用 `after`/`limit`，legacy Trace 使用 `before_sequence`/`limit`，native Trace
使用 `after_sequence`/`limit`（limit 为 1–500），信箱使用 `cursor`/`limit`。
各接口分页格式不同，不要共用一个猜测的游标规则。

Legacy WorkerItem 响应提供 `can_intervene`、`read_only_reason`、`intervention_context` 和
`trace_descriptor`；native 使用独立 `native-context` 响应中的完整 `context` 和执行绑定，
不能从 legacy 七字段示例生成 native 上下文。据此决定操作，不要根据 Worker 名称
或某条日志推测当前会话。缺失的用量或 Trace 保持未知，不补成零或成功。

## 提交操作

用户操作通过 `POST /api/v1/commands` 提交。公共包络含命令 ID、工作空间、actor、
目标对象、预期版本、幂等键和具体 payload；命令名不是任意自然语言。

[命令与介入示例](commands.md)给出完整包络、真实的 `SendExecutionInput` payload
及失败恢复规则。OpenAPI 中的 `payload` 是通用对象，并不单独列出所有业务命令字段；
新增命令接入时还需核对对应实现和测试，不能从包络 schema 推断字段。

收到写入成功，只代表该命令已应用到控制面；实际 Agent 执行、消息投递和其他外部动作
仍需相应执行者处理。新版由产品进程管理其原生执行 owner；模拟 Demo 不运行真实 Agent。

## 主 Agent 与聊天

新版 `/main-agent/v1` 由产品组合的公共 Agents Run 引擎执行。Profile 可作为主 Agent、
Planner 或 Worker；各次工作获得独立 Run 授权。凭据留在服务端，不继承全局 CLI 登录。

`/product/v1/task-tools/mcp` 按原始已接入 Run、当前责任范围及 response 发放授权。
Main 可以提交目标、读规划/任务和请求修订；Worker 只能读取与报告自己的工作，
自报进度为未验证。Agent 工具不能确认规划、验收成果或修改项目设置，普通客户端
不得伪造 Run grant。产品自动提供这些任务工具，不必在 Profile 中手工构造另一套装配。

[Akashic Task Bridge](../../integrations/akashic/README.md)及
[Main Agent Gateway](../../services/main-agent-gateway/README.md)是保留的 legacy 入口。
旧 ACP 迁移独立排期；这些不是新版启动条件，也不是已删除的薄兼容层。

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
- [Agents](../../packages/agents/README.md)：Profile、Runtime、Run 和 Harness Adapter。
- [Product Server](../../apps/server/README.md)：原生装配、本地会话与生命周期。
- [Web](../../apps/web/README.md)：前端运行、API 类型与浏览器测试。
- [Akashic Task Bridge](../../integrations/akashic/README.md)：legacy 主 Agent 适配器。

报告接入问题时提供 SIDERA 提交版本、脱敏请求、HTTP 状态码和错误码即可。
不要提交模型 key、知识正文或完整私人会话。
