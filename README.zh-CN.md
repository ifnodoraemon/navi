# Navi

Navi 是一个本地优先、受治理的个人 AI 助手 Agent OS。

系统向模型提供当前事实和声明式能力，而不是在 Prompt 中固化产品流程。模型负责语义判断；运行时负责 Schema、权限、审批、工作区边界、副作用策略、持久化和审计证据。

[English README](README.md)

## 当前能力

- 基于声明式、Schema 校验的 capability manifest 进行规划。
- 持久化 Goal、Run、审批、循环 checkpoint 和 trace 证据。
- 带类型、作用域、来源、置信度、冲突和撤销能力的记忆系统。
- 权限上限、持久审批、hook、资源门禁和影子工作区。
- CLI 与需要 API Key 的本地 FastAPI 接口。
- 通过 connector registry 加载 Weixin/iLink 和 Telegram 适配器。
- 可检查的 Prompt manifest 和可审计的 evolution ledger。

当前架构和已知边界偏差记录在[当前架构](docs/architecture.md)中。旧计划或发布叙事不能作为已实现行为的依据。

## 快速开始

Navi 需要 Python 3.13 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
navi chat
```

启动需要认证的本地 API：

```bash
navi api
```

API 请求需要 `X-API-Key`，对应 `NAVI_HOME/config.yaml` 中的
`api.api_key`，默认配置文件为 `.navi/config.yaml`。

## 全局配置

`.navi/config.yaml` 是唯一的 Navi 运行时配置文件。`NAVI_HOME` 是唯一
启动环境变量，只用于选择配置目录，不覆盖文件中的值。使用 `navi config`
可以查看自动脱敏后的生效配置。

全局配置分为 `model`、`runtime`、`execution`、`api`、`search`、
`connectors` 和 `mcp` 七个域。模型路由、API 密钥、搜索 provider、连接器
凭据和 MCP server 均在该文件中配置。旧 `.navi/env`、`.navi/mcp.json`
和 `.navi/api_key` 会被明确拒绝，不再合并或兼容。

## CLI

```bash
# 运行时与诊断
navi chat
navi status
navi doctor
navi doctor --connectivity
navi model

# 能力与 Prompt
navi tools list
navi hooks list
navi prompts inspect planner --json-output
navi skills

# 持久状态
navi goal list
navi trace list
navi memory list
navi session list
navi evolution list

# 连接器与服务
navi connectors list
navi connectors status weixin
navi service unit

# 评测数据集
navi eval claw --validate-only --dataset evals/claw_navi.yaml
```

使用 `navi COMMAND --help` 查看当前子命令契约。

## 连接器

配置并运行 Weixin/iLink：

```bash
navi connectors setup weixin
navi connectors run weixin --once
```

真实连接器行为依赖上游 payload 和凭据。连接器测试使用注入的测试替身；Navi 不提供运行时 fake 模式。

## 本地状态

Navi 将本地状态写入 `.navi/` 或自定义的 `NAVI_HOME`。Run 与审批、Goal、循环 checkpoint、trace、记忆、图数据、演进记录和连接器状态目前分布在不同 SQLite store 中。一致性边界和已知缺口见[当前架构](docs/architecture.md)。

## 验证

```bash
pytest -q
ruff check src tests
python -m compileall -q src/navi
python -m build
```

真实模型测试为显式启用项，并要求配置 provider 凭据。

## 权威文档

文档只保留下面这组长期契约。当前 CLI/API 行为以代码和 `--help`
为准；已完成计划、事故记录和专题 checklist 应折入这些文件或删除。

- [产品需求](docs/requirements.md)
- [不可违反原则](docs/principles.md)
- [当前架构](docs/architecture.md)
