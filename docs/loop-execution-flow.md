# Navi Loop 执行流程详解

本文档详细描述了 Navi 当前 Loop 的完整执行机制，严格对应 `loop-engineering.md` 中定义的 Runtime Contract。

## 核心原则

> Loop 控制是**代码级别的运行时决策 (Runtime Decision)**，而非 Prompt 规则。
> 模型不知道 Loop 的存在，它只知道"被调用、返回结果"。

---

## Loop 执行流程图

```mermaid
flowchart TD
    Start["用户输入 (User Message)"] --> Init

    subgraph Init ["① 初始化 (Initialize Turn)"]
        CB["ContextBuilder.initialize_turn()"]
        CB -->|组装| Session["解析 session_id / trace_id"]
        CB -->|组装| Ctx["构建 CapabilityContext"]
        CB -->|组装| Obs["observations = 空列表"]
        CB -->|初始化| PG["progress_gate = LoopProgressGate()"]
    end

    Init --> LoopHead

    subgraph MainLoop ["② 主循环 while True"]
        LoopHead{"observations 过多?"}
        LoopHead -->|">6 条"| Compact["压缩旧 observations\n保留首尾"]
        LoopHead -->|否| ReactStep
        Compact --> ReactStep

        subgraph ReactStep ["③ _react_step: Observe → Plan → Act"]
            Plan["Planner.plan()\n发送 Prompt + Tools + Observations 给 LLM"]
            Plan -->|返回| Syscall["解析 Syscall\n(tool, args, permission)"]

            Syscall --> PlannerCheck{"Planner 是否成功?"}
            PlannerCheck -->|解析失败\nsystem.planner_error| NonTerminalErr["should_return=False\n(可恢复错误,继续循环)"]
            PlannerCheck -->|Provider 崩溃| FatalErr["should_return=True\n(致命错误,立刻退出)"]
            PlannerCheck -->|成功| Invoke["CapabilityRegistry.invoke()\n执行工具调用"]

            Invoke --> BuildResult["构造 _StepResult\n(result, facts, progress_signature)"]
        end

        ReactStep --> Branch{"step_result 分支判断"}

        Branch -->|"should_return=True\n(致命异常)"| Exit1["记录 LoopDecision: FAILED\n记录 trace.final → 退出"]

        Branch -->|"should_continue=True\n(Checker 拒绝了结果)"| Recovery

        subgraph Recovery ["④ Recovery 路径"]
            RecAdd["将 recovery_observation\n追加到 observations"]
            RecAdd --> RecReduce["reduce_recovery_step()\n计算 LoopControlResult"]
            RecReduce --> RecGate{"LoopProgressGate\n检测重复?"}
            RecGate -->|"repeated=True\n(连续3次相同签名\n或 ABAB 链式循环)"| RecFinalize["effect=FINALIZE_STABLE\nDecision: CONVERGED\n→ 强制收敛退出"]
            RecGate -->|"repeated=False"| RecContinue["effect=CONTINUE_LOOP\nDecision: RECOVER\n→ 带 warning 继续循环"]
        end

        Branch -->|"普通结果\n(非致命, 非 recovery)"| NormalPath

        subgraph NormalPath ["⑤ 正常路径"]
            TermCheck{"result.terminal?"}
            TermCheck -->|"是 (模型主动结束)"| Exit2["记录 LoopDecision: FINALIZE\n→ 退出循环"]
            TermCheck -->|"否 (还需继续)"| AppendObs["将 observation\n追加到 observations"]
            AppendObs --> RuntimeReduce["reduce_runtime_step()\n计算 LoopControlResult"]
            RuntimeReduce --> RuntimeGate{"LoopProgressGate\n检测重复?"}
            RuntimeGate -->|"repeated=True"| RuntimeFinalize["effect=FINALIZE_STABLE\nDecision: CONVERGED\n→ 强制收敛退出"]
            RuntimeGate -->|"repeated=False"| RuntimeContinue["effect=CONTINUE_LOOP\nDecision: CONTINUE\n→ 回到循环头部"]
        end

        RecFinalize --> Finalize
        RuntimeFinalize --> Finalize
        RecContinue --> LoopHead
        RuntimeContinue --> LoopHead

        subgraph Finalize ["⑥ 收敛终结 (FINALIZE_STABLE)"]
            ConvTrace["记录 TracePhase.RUNTIME_CONVERGED"]
            ConvTrace --> SurfaceText{"有用户可见文本?"}
            SurfaceText -->|"无"| ModelGen["调用 Responder 模型\n从 facts 生成自然语言回复"]
            SurfaceText -->|"有"| BuildFinal["构造 AgentTurnResult\nterminal=True"]
            ModelGen --> BuildFinal
            BuildFinal --> RecordFinal["记录 trace.final\n记录 turn\n触发 background memory\n→ 返回结果"]
        end
    end

    Exit1 --> End["返回 AgentTurnResult"]
    Exit2 --> End
    RecordFinal --> End

    style MainLoop fill:#fafafa,stroke:#333,stroke-width:2px
    style ReactStep fill:#e3f2fd,stroke:#1565c0
    style Recovery fill:#fce4ec,stroke:#c62828
    style NormalPath fill:#e8f5e9,stroke:#2e7d32
    style Finalize fill:#fff3e0,stroke:#ef6c00
```

---

## 关键组件职责对照表

| 组件 | 文件 | 职责 |
|---|---|---|
| **TurnExecutor.handle_loop()** | `core/turn_executor.py:477` | 外层 `while True` 驱动器。负责调度 `_react_step` 并根据返回值走不同分支。 |
| **TurnExecutor._react_step()** | `core/turn_executor.py:223` | 单步执行器。调用 Planner → 解析 Syscall → 调用 Capability → 返回 `_StepResult`。 |
| **reduce_runtime_step()** | `loop_control.py:153` | **正常路径的 Loop 裁判**。接收每步的 facts 和 progress_signature，产出 `CONTINUE_LOOP` 或 `FINALIZE_STABLE`。 |
| **reduce_recovery_step()** | `loop_control.py:58` | **恢复路径的 Loop 裁判**。当 Checker 拒绝了模型的结果后，判断是继续恢复还是强制收敛。 |
| **LoopProgressGate.observe()** | `loop.py:240` | **死循环检测器 (No-Progress Gate)**。维护签名历史，通过三种策略检测重复：① 同一工具连续3次相同签名 ② ABAB 链式循环 ③ 全局签名出现≥5次。 |
| **LoopDecision** | `loop.py:129` | **结构化决策记录**。每次循环边沿都会产出一条包含 `decision`、`reason`、`failure_domain`、`checker_results`、`gate_results` 的完整决策，持久化到 TraceStore。 |

---

## Loop Decision 状态机

```mermaid
stateDiagram-v2
    [*] --> CONTINUE: 工具调用成功\n未检测到重复

    CONTINUE --> CONTINUE: 新的工具调用\n签名不重复
    CONTINUE --> RECOVER: Checker 拒绝结果\n追加 recovery observation
    CONTINUE --> FINALIZE: 模型调用 respond\n(terminal=True)
    CONTINUE --> FAILED: Provider 崩溃\n或致命异常

    RECOVER --> CONTINUE: 恢复成功\n签名未重复
    RECOVER --> CONVERGED: 恢复失败\n签名重复≥3次

    FINALIZE --> [*]: 正常退出

    CONVERGED --> [*]: 强制收敛退出\n生成 convergence_message

    FAILED --> [*]: 异常退出\n记录 failure_domain
```

---

## 与 loop-engineering.md 的对应关系

| 文章中的概念 | 代码中的实现 |
|---|---|
| `continue` decision | `LoopDecisionKind.CONTINUE` → `reduce_runtime_step` 返回 `CONTINUE_LOOP` |
| `recover` decision | `LoopDecisionKind.RECOVER` → `reduce_recovery_step` 中 Checker 拒绝后的恢复分支 |
| `converged` decision | `LoopDecisionKind.CONVERGED` → `LoopProgressGate` 检测到 `repeated=True` |
| `finalize` decision | `LoopDecisionKind.FINALIZE` → 模型主动调用 `respond` 等终止工具 |
| `blocked` decision | `LoopDecisionKind.BLOCKED` → 审批等待或门禁拦截 |
| `failed` decision | `LoopDecisionKind.FAILED` → Provider 崩溃、引擎异常 |
| `checker_results` | `LoopCheckResult` 数组，包含 `completion_checker`、`capability_result` 等 |
| `gate_results` | `LoopCheckResult` 数组，包含 `no_progress_gate` 等 |
| `progress_signature` | `semantic_progress_signature()` 生成，由 `LoopProgressGate.observe()` 消费 |
| `failure_domain` | `TraceFailureDomain` 枚举：`planner_or_parser`、`capability_failure`、`loop_no_progress` 等 |
| Trace evaluation 优先 loop decisions | `TraceStore` 的 `evaluate` 方法优先读取 `loop_decisions`，而非直接看 raw events |
