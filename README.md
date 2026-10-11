# SIDERA

开发者的多 Agent 任务工作空间。

任务进行到哪了，哪一块卡住了，什么时候需要你介入？SIDERA 把分散的 Agent
工作整理成可以逐层查看的任务空间，让你先掌握进展，需要时再深入执行细节。

你负责目标与判断，Agent 分工执行，SIDERA 帮你看清进展、依赖和需要介入的位置。

> 当前为 **macOS 本机单人、OpenCode 驱动的桌面 Web 预览版**，不是托管服务或正式稳定版。
> 新版源码位于 [`feat/native-harness-modularity`](https://github.com/Aitejiu/SIDERA/tree/feat/native-harness-modularity)。
> 默认 `main` 分支仍保留旧版实现；体验下面的真实执行能力，请使用新版分支。

## 现在能做什么

- **先看全局。** 在任务列表查看整体进度和需要处理的事项，进入任务定位具体子任务的卡点。
- **按需深入。** 按“任务 → 子任务 → WorkerItem”逐层打开。WorkerItem 是一位 Worker 的工作空间，集中呈现当前步骤、工作记录和你的介入消息。
- **两种视角。** 用看板查看谁在执行、谁在等待；切换 DAG 查看并行关系和前后依赖。
- **配置自己的智能体。** 登记本机 OpenCode，创建包含模型、指令、并发和凭据引用的可复用 Profile，查看它的工作历史。能力配置以 Runtime 实际支持的 Skills、MCP 等资源为准。
- **明确分工，再开始执行。** 通过主 Agent 聊天提交目标，由 Planner 生成方案，检查子任务、依赖和负责人后显式确认。子任务和 WorkerItem 可以指定当前用户或 Profile；同一 Profile 可以担任主 Agent、Planner 和 Worker。
- **局部介入与交接。** 在活动 Worker 会话中补充要求或纠正方向，保留消息与投递状态；也可显式调整 Profile 负责人，在原执行停止后交接上下文和工作空间。整体需求变更回到主 Agent 讨论。
- **细节不抢占主界面。** 需要排查时再打开 Agent Trace，查看具体事件及输入输出；多个任务可以在独立标签中切换。
- **验证后再验收。** 汇集执行成果，运行项目中明确配置的检查，由用户查看并接受候选结果。任务、对话、执行记录和事件持久化保存，重启不需要重新导入样例数据。

当前已有 OpenCode 真实主链路的验证，不只是模拟看板。另有无模型演示入口，适合浏览
交互或展示流程；它的 Agent 输出是模拟数据，不能当作真实执行结果。

## 使用范围与限制

| 项目 | 当前状态 |
| --- | --- |
| 使用方式 | macOS、本机单人、桌面浏览器；服务仅绑定 loopback |
| Harness | 内置 OpenCode；其他 Harness 尚未接入 |
| 模型与凭据 | 显式指定账户和模型；macOS 托管凭据需启用 Keychain，不继承全局 CLI 登录，不自动 fallback |
| Worker 介入 | 仅投递给当前可接收输入的活动会话；会话结束后不会自动启动续作 |
| 成果 | 验证和用户验收分开；接受不会自动合并到原仓库分支 |
| Trace 与用量 | 按运行时实际返回展示；部分正文会脱敏或缺失，用量缺失不等于零消耗 |
| 知识库 | 独立可选服务，不是默认安装后即可使用的能力 |
| 团队与远程部署 | 尚不支持邀请、跨用户共享和生产级权限隔离；请勿部署到公网 |

## 安装与启动

需要 Node.js 24、Python 3.12、`uv`、Git、PostgreSQL 16 和本机 OpenCode。
新版不需要 Akashic 或 Docker；真实执行会产生你所选服务商的调用费用。
目前从源码安装，尚未提供一键安装器。

先获取新版源码，安装 Web 依赖和隔离的后端环境：

```bash
git clone --branch feat/native-harness-modularity https://github.com/Aitejiu/SIDERA.git
cd SIDERA
npm --prefix apps/web ci
SIDERA_INSTALL_ROOT="$(mktemp -d /private/tmp/sidera-install.XXXXXX)"
python3.12 scripts/check-agent-module-installation.py \
  --repository "$PWD" --output-root "$SIDERA_INSTALL_ROOT" \
  --combination native-product
export PATH="$SIDERA_INSTALL_ROOT/native-product-venv/bin:$PATH"
```

接着按[安装指南](docs/deployment/initial-demo.md#配置产品环境)创建四个专用数据库、
设置产品环境，并按[凭据设置](docs/deployment/initial-demo.md#配置模型凭据)启用本地凭据入口。
不要跳过环境配置直接启动；产品不会自动读取 `.env` 或导入全局 OpenCode 登录。
准备完成后执行：

```bash
sidera migrate
sidera preflight --json
```

检查通过后，在加载了同一组配置的终端中启动后端：

```bash
sidera serve --host 127.0.0.1 --port 18080
```

另开终端，在仓库根目录启动 Web：

```bash
SIDERA_WEB_BACKEND=native \
SIDERA_WEB_PRODUCT_URL=http://127.0.0.1:18080 \
npm --prefix apps/web run dev -- --port 5173 --strictPort
```

第三个终端加载同一组产品配置和安装环境：

```bash
sidera session bootstrap
sidera open
```

在打开的网页进入“智能体”→“本地会话”，使用 bootstrap 输出的私有文件里的 `token`
连接。不要粘贴文件路径或整份 JSON；该令牌默认五分钟内有效，只能兑换一次。

只想看页面、不希望调用模型？选择[无模型演示](docs/deployment/initial-demo.md#可操作的网页版演示)，
它与上面的真实执行使用独立环境，不要混用数据库或启动命令。

## 第一次使用

1. **配置智能体。** 在“运行环境”登记本机 OpenCode，在“新建 Profile”中配置模型、指令和凭据；需要改代码的 Worker 要明确启用编码工具权限。
2. **安排角色。** 在“默认安排”指定 Main、Planner 和 Worker；可复用同一个 Profile，也可分别配置。Runtime 容量由这些 Profile 共享。
3. **连接项目。** 使用[项目配置命令](docs/deployment/initial-demo.md#配置项目与验证)登记要处理的 Git 仓库、Planner 和真正的验证命令。这一步目前仍需 CLI。
4. **提交并确认。** 与主 Agent 描述目标和验收标准，查看关联规划，确认子任务、依赖和负责人后开始执行。仅收到聊天回复不代表任务已开始。
5. **跟踪并介入。** 日常看任务与子任务的看板；需要理解依赖时切换 DAG。点进 WorkerItem 查看步骤、发送局部要求，必要时展开 Trace。
6. **检查并验收。** 查看成果、变更和项目检查结果，再显式验收；后续是否应用到原项目由你决定。

完整操作和[首个任务示例](docs/guide/workspace.md#试一个有依赖的小任务)见使用指南。请先在可以试验的 Git 项目中体验，
为 Agent 明确配置权限和检查命令，不要把不可恢复的数据交给未验证的自动化执行。

## 接下来要做什么

以下是后续方向，不表示已经可用，也没有固定发布日期：

- [ ] 优先打磨桌面真实使用：任务信息层级、错误提示、历史查看与日常介入体验。
- [ ] 简化安装、配置和升级，完善本地数据备份恢复，优化前端加载体积。
- [ ] 整理并迁移仍有消费者的旧版执行入口，保留任务历史与已接受成果。
- [ ] 接入更多本机 Harness，沿用可复用 Profile 和明确的任务分工。
- [ ] 增加团队成员、授权上下文、对话分享和显式交接。
- [ ] 完善个人记忆、任务上下文与项目知识的审核沉淀和复用。
- [ ] 在桌面体验稳定后适配手机和平板。

## 安全使用

模型 key 通过本地凭据入口配置，不要写入 Profile 指令、聊天、示例或 Git。
当前版本的 Agent 工具权限不等于操作系统沙箱；授权命令执行前先核对项目和权限。
反馈问题时请脱敏代码、路径、数据库连接、Trace 和个人信息，不要上传完整凭据或会话。

## 文档与反馈

- [快速开始](docs/deployment/initial-demo.md)：安装、启动与常见问题。
- [使用指南](docs/guide/workspace.md)：查看任务、定位卡点、介入和阅读 Trace。
- [旧版演示与维护](docs/deployment/initial-demo.md#legacy无模型预置-demo)：无模型演示、既有环境维护及其边界；不是新版安装的必需步骤。
- [集成与 API](docs/api/README.md)：接入自己的 Agent、工具或界面。
- [问题反馈](https://github.com/Aitejiu/SIDERA/issues)：提交使用问题，请勿附带密钥或私人任务内容。
