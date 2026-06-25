# Navi

Navi 是一个**本地优先的个人 AI 助手 Agent OS**。

它的目标不是用关键词和固定流程替 AI 做事，而是给模型提供脚手架：声明式能力、权限上限、审批与治理状态、治理型记忆、连接器上下文、执行 trace、恢复计划和可回滚演进。模型从当前事实和可用能力中选择下一步，运行时负责边界、证据和审计。

`1.1.2` 收紧远程连接器执行边界，补齐受治理 delegation 所需的环境事实工具，并让 workflow 文档/eval 对齐当前运行时契约。

[English README](README.md)

## 核心能力

- **能力驱动运行时**：planner 从 capability manifest 选择 syscall，而不是靠产品关键词路由。
- **受控本地执行**：任务、watch、goal、审批、执行、验证和恢复都有持久记录。
- **审批与治理边界**：用户聊天文本不是执行授权；本地动作必须走明确的审批、权限上限和治理策略。
- **治理型记忆**：支持偏好、约束、事实、反例、语义记忆、来源、置信度、召回原因、冲突和撤销。
- **防御纵深安全**：权限上限、连接器工具策略、声明式能力风险元数据、untrusted observation 边界、trace 评估和 safeguard 归因。
- **受治理动态工作流**：模型根据普通用户请求自行判断是否需要 workflow，并提出声明式 orchestration plan，包含 subagent 步骤、依赖、allowed tools、审批、断点续跑和独立验证。
- **可回滚演进账本**：prompt、记忆、技能、治理策略、工作流和 eval 变化都可审计、可回滚。
- **多入口访问**：CLI、本地 FastAPI、Weixin/iLink 和 Telegram 连接器包；连接器通过 registry 加载，并受 remote-safe tool policy 约束。

## v1 契约

Navi 1.0.0 稳定以下公开 agent OS 契约：

- capability spec、权限和工具调用执行；
- task、watch、goal、approval、recovery、sub-agent 和 trace 记录；
- 治理型 memory item shape 和冲突可见性；
- CLI 与本地 API 控制面；
- 通过 remote tool policy 保护的连接器入口；
- 通过测试、eval、compile check 和文档 trace 支撑的发布验证。

内部兼容债不保留。过时的内部 schema、alias 和 workflow branch 应该删除，而不是静默适配；除非迁移本身就是明确发布的产品功能。

Navi 1.1.0 把 dynamic workflow 作为 v1 系列扩展加入。Workflow plan 是数据，不是可执行脚本：每一步都必须通过运行时调用声明式 capability，并受审批、权限上限、allowed tools、subagent 证据和 verifier 检查约束。

## 快速开始

```bash
cd navi
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
navi chat
```

启动无界面的本地 API：

```bash
navi api
```

本地 API 地址以 `navi api` 的命令输出和当前配置为准。

## 审批工作流

当你提出需要本地执行的请求时，Navi 会先创建受控任务：

1. 准备任务和执行计划。
2. 返回任务 ID、审批码和过期时间。
3. 等你回复 `批准 <审批码>` / `approve <code>`，或通过本地 API 批准。
4. 任务进入队列后，才会由运行时处理。

拒绝任务可以回复 `拒绝 <审批码>` / `reject <code>`。

## 常用命令

```bash
# 聊天
navi chat

# 诊断
navi status
navi doctor
navi doctor --connectivity

# 工具、hook 与 prompt
navi tools list
navi hooks list
navi prompts inspect planner

# 本地 API
navi api

# 记忆
navi memory list
navi memory add preference "我偏好直接给出结论" --reason "用户明确表达偏好" --provenance "manual"
navi memory recall "开发偏好"
navi memory revoke <item_id>

# 任务与演进
navi goal list
navi trace list
navi subagent list
navi workflow list
navi workflow show <workflow_id>
navi evolution list
navi evolution proposals
navi evolution show <event_id>
navi evolution rollback <event_id>

# 会话与技能
navi session list
navi session new [alias]
navi skills
```

本地 API 也提供同一组诊断信息：`/v1/diagnostics`。
如需短超时 API 连通性探测，使用 `/v1/diagnostics?connectivity=true`。

## 评测

Navi 的核心评测贴近日常用户行为，而不是只测内部工具偏好：

```bash
# 工具路由覆盖
navi eval delegations --dataset evals/delegation_cases.yaml

# 用户可见的日常流程
navi eval daily --dataset evals/daily_journeys.yaml

# 面向 Navi 核心流程的 Claw-Eval 风格 Pass^3 任务集
navi eval claw --dataset evals/claw_navi.yaml --attempts 3
```

`evals/claw_navi.yaml` 是仓库内的 Claw-Eval 兼容子集，保留 task/split/rubric/Pass^3 结构，但不把大体积外部 fixture 放进仓库。

## 本地状态目录

Navi 默认把状态写入 `.navi/` 或自定义的 `NAVI_HOME`：

```text
.navi/
├── config.yaml
├── env
├── evolution.db
├── goals.db
├── graph.db
├── memory.db
├── runs.db
├── subagents.db
├── traces.db
├── workflows.db
└── skills/
```

## 微信连接器接入

```bash
# 扫码授权账号后，长轮询拉取消息
navi connectors setup weixin
navi connectors run weixin --once
```

真实微信/iLink 接入仍需要按现场 payload 做校准。连接器逻辑通过注入测试替身在 eval/单元测试中验证，运行时不提供 fake 模式。

## 发布说明

- [Navi 1.0.0 发布说明](docs/release-notes-1.0.0.md)
- [Navi 1.1.0 发布说明](docs/release-notes-1.1.0.md)
- [Navi 1.1.1 发布说明](docs/release-notes-1.1.1.md)
- [Navi 1.1.2 发布说明](docs/release-notes-1.1.2.md)
- [当前需求](docs/requirements.md)
- [不可违反原则](docs/principles.md)
- [前沿 Agent 安全审计](docs/frontier-agent-safety-audit.md)
- [动态工作流](docs/dynamic-workflows.md)
- [Prompt 架构](docs/prompt-architecture.md)
- [版本契约](docs/versioning.md)
