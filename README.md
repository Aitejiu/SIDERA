# SIDERA

开发者的多 Agent 任务中控台。

任务进行到哪了，哪一块卡住了，什么时候需要你介入？SIDERA 把分散的 Agent
工作整理成可以逐层查看的任务空间，让你先掌握进展，需要时再深入执行细节。

## 从任务出发，掌握每一块进展

- **先看全局。** 在任务列表查看整体进度和需要处理的事项，进入任务定位具体子任务的卡点。
- **按需深入。** 按“任务 → 子任务 → WorkerItem”逐层打开。WorkerItem 是一位 Worker 的工作空间，集中呈现当前步骤、工作记录和你的介入消息。
- **两种视角。** 用看板查看谁在执行、谁在等待；切换 DAG 查看并行关系和前后依赖。
- **明确负责人。** 将子任务或 WorkerItem 分配给自己或已配置的 Agent Profile；同一 Profile 可以担任主 Agent、Planner 和 Worker。
- **局部介入。** 在活动 Worker 会话中补充要求或纠正方向，并保留介入记录。单点介入与整体需求变更分开处理。
- **细节不抢占主界面。** 需要排查时再打开 Agent Trace，查看具体事件及输入输出；多个任务可以在独立标签中切换。

## 开始体验

当前提供桌面端、本机单人使用的早期版本。配置本机 OpenCode Runtime、模型凭据和
可复用 Profile 后，可以从聊天提交需求，经规划、确认、执行和验证得到待验收成果。
新版不需要 Akashic 或 Docker；真实执行会产生你所选服务商的调用费用。

跟随[安装与启动](docs/deployment/initial-demo.md)准备环境，再按这个顺序使用：

1. 登记运行环境，创建 Profile，设置默认安排和项目的验证命令。
2. 与主 Agent 沟通目标，查看规划及负责人，确认准确版本后开始执行。
3. 用看板或 DAG 跟踪任务，进入 WorkerItem 查看步骤、介入和 Trace，最后检查并验收成果。

完整的页面操作见[使用指南](docs/guide/workspace.md)。

> 内置 Harness 当前仅有 OpenCode。团队共享、跨用户权限、其他 Harness 和移动端不在
> 当前交付范围内，请勿部署到公网。只想浏览页面，也可以选择快速开始中的无模型模拟
> 演示；它与真实执行是独立入口，模拟状态不能作为模型执行证据。

## 文档与反馈

- [快速开始](docs/deployment/initial-demo.md)：安装、启动与常见问题。
- [使用指南](docs/guide/workspace.md)：查看任务、定位卡点、介入和阅读 Trace。
- [本地维护现状](docs/deployment/initial-demo.md#本地维护现状)：旧演示的显式 inventory 维护入口与恢复边界，不是新版自动备份能力。
- [集成与 API](docs/api/README.md)：接入自己的 Agent、工具或界面。
- [问题反馈](https://github.com/Aitejiu/SIDERA/issues)：提交使用问题，请勿附带密钥或私人任务内容。
