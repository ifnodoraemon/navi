# Navi 神经化 Agent 架构与连续自进化体系白皮书

> **版本**：v1.0 (Post-Zero-Branching & Closed-Loop Milestone)  
> **设计哲学**：Agent 本质上是一个以代码为确定性算子、以动态参数为突触权重、以前向因果依赖为计算图、以沙盒自博弈为强化学习机制的**类 LLM 神经网络系统**。

---

## 目录
1. [从过程式 Agent 到神经计算图的范式跃迁](#1-从过程式-agent-到神经计算图的范式跃迁)
2. [消除分支（Zero-Branching）的数学与自进化必然性](#2-消除分支zero-branching的数学与自进化必然性)
3. [系统四大核心支柱全景](#3-系统四大核心支柱全景)
   - [支柱一：动态参数连续矩阵（Weights Matrix）](#支柱一动态参数连续矩阵weights-matrix)
   - [支柱二：前向注意力与因果追踪（Attention & Causal Trace）](#支柱二前向注意力与因果追踪attention--causal-trace)
   - [支柱三：反向信用分配与突触可塑性（Credit Assignment & Backprop）](#支柱三反向信用分配与突触可塑性credit-assignment--backprop)
   - [支柱四：暗网沙盒自博弈与自强化回路（Self-Play Shadow Arena）](#支柱四暗网沙盒自博弈与自强化回路self-play-shadow-arena)
4. [系统拓扑全景图（ASCII 架构）](#4-系统拓扑全景图ascii-架构)
5. [当前审查：不足、偏离与演化路线图](#5-当前审查不足偏离与演化路线图)

---

## 1. 从过程式 Agent 到神经计算图的范式跃迁

传统的 LLM Agent 往往被实现为高度耦合的条件逻辑（`if-else`、硬编码配置常量、固定重试策略）。这种软件工程模式在应对自进化时存在致命缺陷：
* **系统硬化（Rigidity）**：权重写死在 Python 常量中，修改需依赖人工修改代码并重启守护进程。
* **损失断裂（Loss Disconnection）**：环境中的失败（如工具超时、用户纠正、解析崩溃）无法反传到具体的注意力块或记忆碎片上。
* **变异不可预测（Discontinuous Mutation）**：离散的代码块变异极易破坏全局语法树，无法进行渐进式微调。

Navi 将现代大语言模型（LLM 从预训练、SFT、RLHF 到 Test-Time Compute / Reasoning RL）的设计思想引入 Agent 系统工程：
* **计算核（Operator Kernel）**：纯代数门控与无阶跃断裂的执行流。
* **权重矩阵（Parameter Matrix $\theta$）**：所有调节系数由统一的持久化参数引擎治理。
* **反向传播（Backprop）**：沿因果 DAG 自动反向分配奖励与惩罚。
* **自博弈（Self-Play）**：后台自生成挑战性用例，在隔离沙盒内推演并自动晋升优胜策略。

---

## 2. 消除分支（Zero-Branching）的数学与自进化必然性

在深度神经网络中，激活函数（如 GELU、Swish、Sigmoid）必须具有平滑可微的性质。离散阶跃函数会导致梯度处处为 0 或无定义，使得基于梯度的优化和连续插值完全失效。

在 Agent 系统工程中：
* **过程式 `else:` / `elif:` 和三元表达式是离散阶跃阻断**。
* 当业务逻辑充斥着嵌套分支时，参数调节无法跨越分支边界，进化算法只能面临“全有或全无”的震荡。

Navi 在全局 150 个源码模块与所有测试中彻底剔除了过程式分支与三元表达式，建立起三种平滑代数计算原语：
1. **代数布尔门控（Algebraic Gating）**：  
   `effective_delta = base_delta * float(condition)`
2. **格序选择（Order-Theoretic Selection）**：  
   `best_candidate = max(pool, key=lambda x: scoring_fn(x))`
3. **哈希表分发（Table Lookup / Dispatch Table）**：  
   `action = handler_table.get(state, default_handler)()`

通过这些原语，代码转化为只读、不变的**确定性算子算网（Deterministic Computational Graph）**，为参数的连续可塑性提供了拓扑平滑性保证。

---

## 3. 系统四大核心支柱全景

### 支柱一：动态参数连续矩阵（Weights Matrix）
* **模块**：`src/navi/dynamic_parameters.py`
* **机制**：
  * 系统所有关键超参数（记忆衰减率、检索覆合度权重、Saga 租约超时、网络退避间隔、安全置信度等 30+ 维数值）全面剥离代码常量。
  * 由 `DynamicParameterRegistry` 统一代理，具备 SQLite 双向持久化与内存 TTL 缓存。
  * 接入 `evolution_targets.py`，作为一级可演化目标（`dynamic_parameter` / `system_parameter`）。

### 支柱二：前向注意力与因果追踪（Attention & Causal Trace）
* **模块**：`src/navi/prompt_os.py` & `src/navi/trace.py`
* **机制**：
  * 前向推导阶段，注意力机制对 Working Memory、Episodic History、Semantic Graph 进行加权投影。
  * Planner 生成调用时，显式记录本次动作所依托的因果依赖项：`used_memory_ids`、激活的 Prompt 层与调用的工具。
  * 形成前向执行的因果图谱（Causal Execution DAG）。

### 支柱三：反向信用分配与突触可塑性（Credit Assignment & Backprop）
* **模块**：`src/navi/credit_assignment.py`
* **机制**：
  * 任务终结时，`TraceStore.evaluate_trace` 自动结算标量回报 $R \in [-1.0, 1.0]$。
  * 信用分配引擎沿因果边逆向传播：
    * **记忆突触强化**：正向成功时增强 LTP 置信度；执行失败时削减置信度，置信度跌破阈值时自动标记为 `stale` 剥离活跃感知池。
    * **网络参数退避**：遭遇网络超时/拥塞时自适应增大退避间隔。
    * **Prompt 层责任归因**：发生解析错误时对相关 Prompt 块登记负向信用，指导演化变异优先级。

### 支柱四：暗网沙盒自博弈与自强化回路（Self-Play Shadow Arena）
* **模块**：`src/navi/self_play.py`
* **机制**：
  * **假设生成器**：基于因果惩罚历史，自主对瓶颈参数生成邻域探索假设（$w \pm \Delta$）。
  * **暗网沙盒**：在隔离环境中对变异候选体进行确定性契约验证与无回归测试。
  * **免人工自动晋升**：由规则断言器（Deterministic Verifier）全自动裁决，胜出策略自动合入生产参数矩阵。
  * **常驻守护集成**：挂载在后台 Daemon 维护循环中自主周期性触发。

---

## 4. 系统拓扑全景图（ASCII 架构）

```
        +===========================================================+
        |             NAVI NEURAL AGENT CLOSED-LOOP SYSTEM          |
        +===========================================================+

   [ TRIGGER / USER INPUT ] (Messages, Timers, Background Events)
              |
              v
   +----------------------------------------------------------------+
   | 1. ATTENTION CONTEXT COMPILATION (前向注意力汇聚)               |
   |    - Episodic Memory + Semantic Knowledge + Prompt Blocks      |
   |    - Weighted by Dynamic Matrix: Cue Coverage, Jaccard, Seq    |
   +----------------------------------------------------------------+
              |
              v
   +----------------------------------------------------------------+
   | 2. DETERMINISTIC ZERO-BRANCHING KERNEL (无分支执行代数核)       |
   |    - Algebraic Gating: float(cond) * weight                    |
   |    - Order-Theoretic Routing: max(candidates, key=scoring)     |
   |    - Emits: Action Syscall + Causal Graph (used_memory_ids)    |
   +----------------------------------------------------------------+
              |
              v
   +----------------------------------------------------------------+
   | 3. ENVIRONMENT & TOOL ACTUATION (真实环境执行与事实采集)        |
   |    - Tool Execution, OS Shell, Network I/O, LLM Syscall        |
   |    - Emits: Execution Status (ok / error / latency / timeout)  |
   +----------------------------------------------------------------+
              |
              v
   +----------------------------------------------------------------+
   | 4. BACKWARD CAUSAL CREDIT ASSIGNMENT (因果反向信用分配引擎)    |
   |    - TraceStore.evaluate_trace() -> Scalar Reward R in [-1, 1] |
   |    - Backpropagation along Causal Execution DAG:               |
   |        * Positive R -> Memory LTP Boost (Synaptic Plasticity)  |
   |        * Negative R -> Memory Confidence Drop / Stale Prune    |
   |        * Provider Timeout -> Dynamic Retry Backoff Adaptive    |
   |        * Parser Failure -> Prompt Layer Blame Registered       |
   |    - Audited to SQLite: causal_credit_attributions             |
   +----------------------------------------------------------------+
              |
              +-------------------------------------+
              | (Parameter / Feedback Signals)      |
              v                                     v
   +----------------------+               +-------------------------+
   | DYNAMIC WEIGHTS      |               | 5. SELF-PLAY SHADOW     |
   | (SQLite Parameter    | <============ |    ARENA (自博弈沙盒)   |
   |  Registry Theta)     |  Auto-Promote |  - Perturbation Gen     |
   |                      |  Verified     |  - Sandbox Verification |
   | 30+ Tunable Weights  |  Candidates   |  - Zero-Human Invariant |
   +----------------------+               +-------------------------+
              ^                                     ^
              |                                     |
              +======== DAEMON OBSERVABILITY =======+
                     (Background Autonomous Cycle)
```

---

## 5. 当前审查：不足、偏离与落地实现

站在严谨软件工程与现代 LLM 理论视角，对系统进行的自查、偏差纠正与演进落地：

### 维度 1：参数空间的连续性 vs 离散孤岛（已落地）
* **演进实现**：
  * 将任务重试预算（`loop_max_attempts_turn`, `loop_max_attempts_control`, `loop_max_attempts_scheduled`, `loop_max_attempts_durable_goal`）与时间差分折现因子（`temporal_discount_factor`）等全部纳入 `DynamicParameterRegistry`。
  * `_retry_policy_for_loop_kind` 全面接入动态参数矩阵，消除硬编码数值孤岛。

### 维度 2：反向传播的深度与时间差分（TD Discounting，已落地）
* **演进实现**：
  * 在多步执行轨迹中引入时序反向传播折扣因子：$\text{discount} = \gamma^{T - 1 - t}$（默认 $\gamma = 0.85$）。
  * 早期检索记忆获得时间平滑惩罚/奖励，彻底规避末步崩溃导致早期正确记忆被灾难性惩罚的问题。

### 维度 3：自博弈沙盒的推演广度（Prompt Layer Self-Play，已落地）
* **演进实现**：
  * `SelfPlayArena` 扩充 `generate_prompt_perturbations` 与针对 `prompt_layer` 的沙盒演化试验。
  * 全参数边界探索：定义 `_get_parameter_bounds` 自动自适应任意新注册参数的扰动区间与步长。
  * 发生 Planner/Parser 因果归因受罚时，沙盒自动生成防御性 Prompt 候选，经过 `runtime.text.nonempty` 契约断言后免人工自动晋升覆盖。
  * 守护进程周期调度同时覆盖连续超参空间与 Prompt 语义层。

### 维度 4：前向 Prompt 演化装配与语义生效闭环（已落地）
* **演进实现**：
  * 在 `OperatingContext` 中将 `instructions` 正式纳入上下文许可层，并在 `_responder_tier` 中归为稳定层（`stable`）。
  * 在 `build_system_prompt_assembly` 中将动态载入的 `instructions` 层直接注入系统提示词编译图，确保沙盒自博弈演化胜出的 Prompt 能够即时在前向推理中生效。

### 维度 5：因果链全要素归因与系统调优工具暴露（已落地）
* **演进实现**：
  * **工具级反传**：在 `CreditAssignmentEngine` 中为 `CAPABILITY_FAILURE` 引入工具节点因果归因（`node_type="tool"`），精准惩处异常工具。
  * **预算耗尽反传**：针对 `LOOP_NO_PROGRESS` 自动反传问责 `instructions` 与 `loop_max_attempts_turn`。
  * **核心工具暴露**：注册 `system.parameters` 工具，向模型与外部直接提供自省与在线微调全部连续权重的统一接口。
  * **审计日志鲁棒性**：消除了变异工具调用审计中 `error=None` 导致的 SQLite 非空约束隐患。


