# 快速开始

在 macOS 上启动 SIDERA 本地 Demo，用样例任务体验完整的页面浏览与介入记录。
不需要模型 API key，也不会产生模型调用费用。

## 可以体验什么

- Task 总览和具体子任务卡点；看板与 DAG 表达状态、并行和依赖。
- `Task -> Subtask -> WorkerItem` 逐层进入，多个任务标签独立保留阅读状态与草稿。
- WorkerItem 的步骤、历史介入和当前 Session；Trace 入口、事件筛选、详情及旧记录分页。
- 将局部介入写入演示数据库，以及显式确认停止某次执行。
- 知识样例、上下文包，以及聊天和信箱的页面入口。

演示数据由现有测试夹具写入真实 PostgreSQL，页面读取真实控制面 API。
其中的“运行中”“已完成”、用量与 Trace 不代表正在调用模型。
此入口不启动 Main Agent、Planner、Scheduler、Worker Gateway 或通知投递进程，
因此无需模型 key，也不会自动执行新任务或发送私聊消息。
介入显示为已入队或待处理时，不应理解为 Worker 已读取或完成。

## 环境要求

- Node.js 24、npm、Python 3.12、`uv`、Git。
- PostgreSQL 16，监听 `127.0.0.1:55432`，已存在本地角色 `sidera`。
- 当前终端用户可以创建数据库并将 owner 指定为 `sidera`。
- 浏览器。

PostgreSQL 首次初始化可参考[控制面运行说明](../../services/control-plane/README.md#native-postgresql)。
下面不替换数据库认证配置；连接需要密码时应使用本机安全配置，例如 `.pgpass`，
不要把实际密码或模型 key 写进仓库。本地 trust 认证只适用于个人 loopback 开发环境。

## 安装与准备数据

在项目根目录执行；已有仓库无需再次 clone：

```bash
git clone https://github.com/Aitejiu/SIDERA.git
cd SIDERA
uv sync --project services/control-plane --all-groups --frozen
uv sync --project services/knowledge-service --all-groups --frozen
npm --prefix apps/web ci
```

先确认 PostgreSQL 可用，再为 Demo 新建两套专用数据库：

```bash
pg_isready -h 127.0.0.1 -p 55432
createdb -h 127.0.0.1 -p 55432 -O sidera sidera_demo_test
createdb -h 127.0.0.1 -p 55432 -O sidera sidera_demo_knowledge_test
```

若提示同名数据库已存在，先确认它们确实是可重置的演示库，再继续。
不要改用日常任务库。以下 seed 会重置固定 ID 的样例工作空间及其关联记录，
因此也不要在这个工作空间保存需要长期保留的数据。

```bash
export SIDERA_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_demo_test"
export SIDERA_KNOWLEDGE_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_demo_knowledge_test"

uv run --frozen --project services/control-plane alembic \
  -c services/control-plane/alembic.ini upgrade head
uv run --frozen --project services/knowledge-service alembic \
  -c services/knowledge-service/alembic.ini upgrade head

SIDERA_ALLOW_E2E_SEED=1 uv run --frozen --project services/control-plane python \
  services/control-plane/tests/e2e/seed_r4_workspace.py --scenario context-knowledge
SIDERA_ALLOW_E2E_SEED=1 uv run --frozen --project services/knowledge-service python \
  services/knowledge-service/tests/e2e/seed_r5_3_knowledge.py
```

重置演示时可再次运行上面两个 seed 命令；它们会覆盖样例中的本地介入等修改。

## 启动三个进程

分别使用三个终端，工作目录均为项目根目录。下列固定 token 仅用于隔离的本地演示，
不是 provider key，不可用于公网或真实共享环境。不要加载已有真实运行栈的环境文件。

终端一，控制面 API：

```bash
SIDERA_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_demo_test" \
SIDERA_WEB_ORIGIN="http://127.0.0.1:18626" \
SIDERA_KNOWLEDGE_BASE_URL="http://127.0.0.1:18627" \
SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN="r5-e2e-control-plane-token" \
SIDERA_CONTEXT_PACKAGE_CUTOVER_AT="2026-08-27T00:00:00+08:00" \
uv run --frozen --project services/control-plane uvicorn \
  sidera_control_plane.api.app:create_app --factory --host 127.0.0.1 --port 18625
```

终端二，知识服务：

```bash
SIDERA_KNOWLEDGE_DATABASE_URL="postgresql+psycopg://sidera@127.0.0.1:55432/sidera_demo_knowledge_test" \
SIDERA_KNOWLEDGE_LOCAL_OWNER_TOKEN="r5-e2e-owner-token" \
SIDERA_KNOWLEDGE_MAIN_AGENT_TOKEN="r5-e2e-main-agent-token" \
SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN="r5-e2e-control-plane-token" \
uv run --frozen --project services/knowledge-service uvicorn \
  sidera_knowledge.api.app:app --host 127.0.0.1 --port 18627
```

终端三，Web：

```bash
SIDERA_WEB_CONTROL_PLANE_PORT=18625 \
SIDERA_WEB_KNOWLEDGE_SERVICE_PORT=18627 \
SIDERA_WEB_MAIN_AGENT_PORT=18628 \
SIDERA_WEB_KNOWLEDGE_TOKEN="r5-e2e-owner-token" \
npm --prefix apps/web run dev -- --port 18626 --strictPort
```

打开 <http://127.0.0.1:18626/workspaces/20000000-0000-0000-0000-000000000001/tasks>。
Workspace UUID 是固定样例标识，不是用户的真实身份。

建议依次查看“支付重构”、子任务“回调幂等”、Worker“实现者”，再打开 Agent Trace。
返回根任务切换 DAG，或打开另一个任务检查标签切换。介入仅影响当前演示记录；
整体需求变更和真实主 Agent 对话不属于这条无模型路径。

健康检查：

```bash
curl --fail http://127.0.0.1:18625/healthz
curl --fail http://127.0.0.1:18627/health/live
```

停止时在上述三个终端分别按 Ctrl+C，不会删除数据库。
若端口被占用，不要结束来源不明的进程；改用空闲端口，并同步 API origin 与 Web proxy 配置。

页面中的日常操作见[使用指南](../guide/workspace.md)。

## 常见问题

### 页面打不开，或者任务加载失败

确认三个终端都没有退出，检查上面的两个健康地址，再核对 Web 地址与代理端口。
端口冲突时同时调整对应服务和代理配置，不要结束不属于本次 Demo 的进程。

### 数据库连接失败

先运行 `pg_isready -h 127.0.0.1 -p 55432`。若数据库已启动，再检查角色、数据库名和
本机认证配置。两套数据库都必须完成迁移，样例数据也需要分别导入。

### 为什么介入一直没有回复，聊天也不可用

默认 Demo 不运行真实 Agent，只保存介入记录。主 Agent 端口 `18628` 未启动，
所以聊天不可用，信箱也可能为空。这不影响任务、看板、DAG 和 Trace 的浏览。

### 重启会丢失任务吗

停止 Web 和 API 不会删除 PostgreSQL 数据。重新运行 seed 会重置样例工作空间，
包括你在其中留下的介入记录，因此不要把需要保留的工作放进样例空间。

### 可以接入自己的 Agent 吗

已有的接入方式和配置入口见[集成与 API](../api/README.md)。真实规划、执行和外部消息
需要额外配置服务、运行模块和凭据，不包含在这条无模型 Demo 路径中。
启用前请确认 provider、模型和计费账户；凭据只放在本地受保护的配置中。

### 可以直接部署到公网吗

不可以。当前版本仅限本机单人使用，尚不具备生产级认证和租户隔离，
也尚未完成完整真实链路验收。请将服务保持在 `127.0.0.1`。
