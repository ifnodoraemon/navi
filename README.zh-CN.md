# Navi（本地优先个人 AI 助手）

Navi 是一个面向开发者和高频电脑用户的本地优先个人 AI 助手。它把对话、任务、审批、记忆、连接器和可回滚演进放在同一个本地运行时里，目标是让 AI 能帮你处理真实工作，同时把权限、执行记录和本地状态留在你自己的机器上。

[English README](README.md)

## 核心能力

- **受控本地任务**：通过 `task.create` 先准备任务和执行计划，再生成审批码；只有在你批准后，任务才会进入执行队列。
- **审批与信任治理**：默认未知任务走 L2 审批，只有命中明确的信任规则和项目范围时才允许更高自治。
- **主动记忆**：支持偏好、约束、事实、反例和语义记忆，并从对话和任务日志中提取可复用上下文。
- **可回滚演进账本**：记忆整理、技能添加、信任规则变化和自演进事件都会写入本地账本，便于审计和回滚。
- **多入口访问**：支持 CLI、无界面的本地 FastAPI，以及个人微信连接器的基础运行链路。
- **技能发现**：从 `.navi/skills/*/SKILL.md` 加载本地技能说明，并按权限上限注入上下文。

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

默认地址通常是 `http://127.0.0.1:8765`。如果你设置了不同的 host 或 port，以命令输出为准。

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

# 本地 API
navi api

# 记忆
navi memory list
navi memory add preference "我偏好直接给出结论"
navi memory recall "开发偏好"
navi memory revoke <item_id>

# 任务与演进
navi evolution list
navi evolution show <event_id>
navi evolution rollback <event_id>

# 会话、技能与信任规则
navi session list
navi session new [alias]
navi skills
navi trust list
navi trust set <rule_id> <autonomy_level>
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
├── graph.db
├── memory.db
├── tasks.db
├── trust.db
└── skills/
```

## 微信连接器本地测试

```bash
NAVI_WEIXIN_MOCK=true navi connectors setup weixin
NAVI_WEIXIN_MOCK=true navi connectors run weixin --once
```

真实微信/iLink 接入仍需要按现场 payload 做校准；本地 mock 用于验证运行链路和策略行为。
