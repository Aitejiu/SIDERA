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

## 可操作的网页版演示

上面的入口适合浏览预置数据。如果要在面试或评审中演示“提交需求 → 规划 →
确认 → 看板跟踪 → WorkerItem 介入 → Trace 与结果”的主链路，请使用隔离的
Interview Demo。它仍然只绑定本机控制面、知识服务和 Web，不调用模型，也不连接
Telegram 或其他外部消息通道；准备阶段会通过公开 API 建立可复现的本地场景。

先在项目根目录准备一个新 run。`SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL`
只用于创建和迁移本次演示数据库；如果本机应用角色已经有 `CREATEDB` 权限可以省略。
它不是 provider key，也不要把带密码的连接串写入仓库：

```bash
export SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL="postgresql+psycopg://local-admin@127.0.0.1:55432/postgres"
export SIDERA_INTERVIEW_DEMO_MANIFEST="$(uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli prepare --scenario mainline --print-manifest-path)"
```

常用操作如下：

```bash
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli start --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli present --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli status --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli doctor --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli open --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
```

`present` 会启动本 run 并把场景准备到公开的停靠点；主线停在空任务，现场再由
用户在网页提交需求。`continue --gate parallel`、`before-artifact` 或 `handoff`
只释放当前演示的受控输出门，不代替用户提交需求、确认计划或接受结果：

真实 basic-acceptance bootstrap 为新创建的 Worker ModuleInstance 显式写入
`worker.completion_policy=initial_turn`。它只表示初始 Agent 回合已经结束，仍需经过
成果检查和用户验收；历史 ModuleInstance 不会被原地改写，旧配置继续按 `explicit` 行为运行。

```bash
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli continue --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST" --gate parallel
```

演示结束只停止这个 manifest 对应的进程，数据库、事件、Trace 和结果会保留：

```bash
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli stop --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
```

需要重新开始时使用 `reset`。它会先停止旧 run，再创建新的隔离 run；输出中的新
`manifest_path` 才是下一次操作的入口。不要用 seed 重置 Interview Demo，也不要
结束其他项目的本地进程：

```bash
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli reset --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
```

Interview Demo 的“运行中”、用量、Worker 输出和 Agent 行为是受控的本地模拟数据，
控制面 API、权限边界、状态变更和页面交互是真实的。它适合展示产品主链路，不代表
生产环境已经完成认证、租户隔离、真实模型执行或公网部署。

## R6 验收与长期交互 Demo

R6 basic stack 有两种明确的运行模式。默认的 `acceptance` 模式只用于收集有预算、
有 route 约束且可审计的真实验收证据；已经完成验收后，如果要继续在同一桌面工作空间
中演示聊天、知识库和任务能力，显式切换到 `interactive` 模式。交互模式复用同一份
manifest、数据库、Main Agent、Planner 和 Worker 配置，不替换成模拟 Agent，也不创建
或延长 acceptance evidence。它会用独立的本地 Worker heartbeat 维持桌面执行节点在线，
同时移除验收专用 heartbeat、预算与 Guard 注入。

两种模式都只监听本机 loopback，不是生产部署或公网服务。每次 `start`、`status`、
`restart` 和 `stop` 都要使用相同的运行目录和模式；manifest 中的 `env:` 数据库引用
必须存在于当前 shell。provider 凭据继续由本机受保护的 credential store 提供，不要把
key、带密码的连接串或 token 写入仓库。

交互模式只复用已经由验收初始化完成的数据库资源，不会重新执行验收 seed，也不会
覆盖用户随后在 Web 中保存的 Main Agent 或 Worker 配置。首次初始化仍应先完成对应
acceptance manifest 的准备流程。

已有验收 manifest 切换为桌面交互 Demo 时，使用绝对路径替换下面的占位值：

```bash
export SIDERA_R6_BASIC_MODE=interactive
export SIDERA_R6_BASIC_RUNTIME_DIR=/absolute/path/to/runtime
export SIDERA_R6_BASIC_MANIFEST=/absolute/path/to/manifest.json
export SIDERA_R6_BASIC_AKASHIC_CONFIG=/absolute/path/to/akashic-config.toml

scripts/r6-basic-stack.sh start --manifest "$SIDERA_R6_BASIC_MANIFEST"
scripts/r6-basic-stack.sh status --manifest "$SIDERA_R6_BASIC_MANIFEST"
```

需要重启时保留同样的环境变量：

```bash
scripts/r6-basic-stack.sh restart --manifest "$SIDERA_R6_BASIC_MANIFEST"
```

不要用交互模式生成新的验收结论，也不要在验收运行仍需要继续取证时切换模式。

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

## 本地维护现状

Interview Demo 可使用本次 run 的 manifest 检查运行状态和本机环境；这些检查不调用模型：

```bash
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli status --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
uv run --directory services/control-plane --frozen python -m sidera_control_plane.demo.cli doctor --manifest "$SIDERA_INTERVIEW_DEMO_MANIFEST"
```

`scripts/local-maintenance.py` 是另一条 provider-free 的本地维护入口，面向已经准备好
source manifest 和完整 component inventory 的 SIDERA 安装。它不会从 Interview Demo
manifest 自动推断数据库、主 Agent、bridge 或 Git 组件，也不会递归扫描项目目录；未在
inventory 中明确声明的持久化组件会被拒绝。Interview Demo 仍然不会自动生成维护清单，
所以不能把 Demo manifest 直接当成备份输入。

当前支持的维护范围：

- 已停止 writer 的 loopback PostgreSQL 数据库，通过 `pg_dump` 导出并在新数据库恢复。
- 已声明的 Akashic/Main Agent SQLite、bridge metadata 和 Git repository bundle；Git
  快照会保留可验证的 head/tree、工作树状态和受支持的 tracked internal symlink。
- 备份后用 `inspect-backup` 校验 payload 与 manifest；恢复前用 `restore-preflight`
  检查目标根目录为空、数据库身份不同、schema identity 匹配且目标为 `read_only`。
- 恢复只写入新的目标根和新的 loopback 数据库。恢复完成后，服务必须显式以
  `restore_read_only=true` 和 `restore_schema_validation=true` 启动；当前迁移必须由
  运维步骤显式运行，不能由只读服务启动时暗中执行。

维护入口不复制 provider key、模型 token、auth 数据库、系统 Keychain 或其他被排除的
秘密；恢复结果要求重新配置这些能力。普通 external/untracked symlink、共享 Git
common dir、非 loopback 目标和未停止的 writer 会 fail closed。它不是整个用户主目录的
通用快照，也没有把回退实现成对升级数据库的原地 downgrade；回退应从未改写的备份恢复
到另一个独立目标，再使用对应旧代码读取。

只有在已经准备好绝对路径的 source manifest、可选的 companion inventory、备份目录和
全新 target manifest 后，才运行以下命令。不要把示例路径或日常数据库直接替换成目标：

```bash
export SIDERA_MAINTENANCE_SOURCE_MANIFEST="/absolute/path/source-manifest.json"
export SIDERA_MAINTENANCE_SOURCE_INVENTORY="/absolute/path/source-inventory.json"
export SIDERA_MAINTENANCE_BACKUP_PATH="/absolute/path/sidera-backup"
export SIDERA_MAINTENANCE_TARGET_MANIFEST="/absolute/path/target-manifest.json"

uv run --directory services/control-plane --frozen python ../../scripts/local-maintenance.py doctor \
  --manifest "$SIDERA_MAINTENANCE_SOURCE_MANIFEST"
uv run --directory services/control-plane --frozen python ../../scripts/local-maintenance.py backup \
  --manifest "$SIDERA_MAINTENANCE_SOURCE_MANIFEST" \
  --source-inventory "$SIDERA_MAINTENANCE_SOURCE_INVENTORY" \
  --destination "$SIDERA_MAINTENANCE_BACKUP_PATH"
uv run --directory services/control-plane --frozen python ../../scripts/local-maintenance.py inspect-backup \
  --backup "$SIDERA_MAINTENANCE_BACKUP_PATH"
uv run --directory services/control-plane --frozen python ../../scripts/local-maintenance.py restore-preflight \
  --backup "$SIDERA_MAINTENANCE_BACKUP_PATH" \
  --target-manifest "$SIDERA_MAINTENANCE_TARGET_MANIFEST"
uv run --directory services/control-plane --frozen python ../../scripts/local-maintenance.py restore \
  --backup "$SIDERA_MAINTENANCE_BACKUP_PATH" \
  --target-manifest "$SIDERA_MAINTENANCE_TARGET_MANIFEST"
```

`target-manifest.json` 至少需要新的 `run_id`、新的 `run_root`、`restore_mode: read_only`、
两个与来源不同的 loopback PostgreSQL target ref，以及与备份组件匹配的 schema identity。
`restore` 不会启动 API、Worker、通知或模型；导入完成后仍需按只读运行说明单独启动查询服务。

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

### 已通过验收的 R6 Demo 为什么聊天返回 `429 deadline_exceeded`

这表示该 run 仍在 `acceptance` 模式，而它的验收预算已经结束。停止后使用上面的
`SIDERA_R6_BASIC_MODE=interactive` 重新启动同一个 manifest；不要修改 acceptance
状态文件，也不要通过放宽服务内部 Guard 来解决。

### 知识库页面为什么返回 `403 permission_denied`

Web 代理和 knowledge-service 必须使用同一个 local-owner capability token。token 只
应该由 Vite 服务端代理注入，不能放进浏览器代码或 URL。若仍返回 `403`，确认 Web 和
knowledge-service 都是通过同一个 R6 stack manifest 启动的，不要把知识服务改成接受
未知角色的 token。

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
