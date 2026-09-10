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

### 维度 6：信息安全防线与香农熵动态治理（已落地）
* **演进实现**：
  * 在 `src/navi/safeguards.py` 中构建香农熵检测算法，激活动态超参数 `safeguards_entropy_threshold`（默认 4.5）。
  * 自动拦截并脱敏长链无前缀高熵密钥（如 Base64 随机凭证、密码学 Token），杜绝敏感信息外泄。

### 维度 7：执行引擎租约生命周期动态化（已落地）
* **演进实现**：
  * 在 `src/navi/loop_runs.py` 中将 Loop 执行声明与争抢租约（`claim_for_execution`）由硬编码 180.0s 切换为从 `SYSTEM_DYNAMIC_PARAMETERS.get("saga_lease_timeout_turn", 120.0)` 动态获取。
  * 消除分布式/守护轮询争抢中的硬编码静态常量，全面实现弹性时间可塑性。

### 维度 8：全域超参连续可塑性与双向信用强化（已落地）
* **演进实现**：
  * **动态化剩余所有静态数值孤岛**：
    * 守护后台轮询间隔：`daemon_poll_interval_seconds` (60.0s)
    * 瞬态执行归档保存期：`transient_retention_seconds` (86400.0s)
    * 前向事实投影深度与字符预算：`planner_fact_max_depth` (8.0), `model_fact_max_chars` (48000.0), `model_fact_max_string_chars` (4000.0), `model_fact_max_depth` (5.0), `model_fact_max_items` (30.0)
    * 上下文证据检索边界：`context_evidence_max_items` (20.0), `context_evidence_excerpt_chars` (700.0), `context_recent_message_limit` (6.0)
    * 投递发件箱重试与沉寂阈值：`outbox_max_attempts` (3.0), `outbox_stale_sending_seconds` (300.0)
    * 子代理工作资源配额：`child_max_active` (3.0), `child_max_timeout_seconds` (900.0), `child_max_token_budget` (50000.0), `child_max_call_budget` (12.0), `child_max_cost_budget` (2.0), `child_max_qps` (5.0)
    * 标量奖赏空间与各失败域严重度：`reward_success` (1.0), `reward_degraded` (0.2), `severity_safeguard_policy` (1.0), `severity_loop_no_progress` (0.8), `severity_planner_or_parser` (0.7), `severity_checker_blocked` (0.6), `severity_capability_failure` (0.5), `severity_runtime` (0.4), `severity_provider_no_response` (0.3), `severity_default` (0.5)
  * **双向信用归因引擎（Bidirectional Credit Assignment）**：
    * 成功执行轨迹：对执行成功的工具节点回传正向奖赏（`node_type="tool"`, `reward=+1.0`），形成正向因果信用沉淀。
    * 安全防线违规：对 `SAFEGUARD_POLICY` 拦截自动反传问责 `instructions` 与动态参数 `safeguards_entropy_threshold`。
  * **自博弈全景参数探索（Self-Play Bound Coverage）**：
    * `_PARAMETER_EXPLORATION_BOUNDS` 涵盖所有新增超参，赋能影子试验场（`ShadowSelfPlayArena`）全闭环自动参数演进与免人工晋升。

### 维度 9：执行计算图微观常量动态化与因果域 Prompt 变异特化（已落地）
* **演进实现**：
  * **状态转移计算图（State Graph）全要素动态化**：
    * 上下文装配与历史截断：`planner_context_message_limit`, `planner_context_recent_messages`, `planner_context_max_chars`, `planner_context_older_preview_messages`, `planner_context_older_preview_chars`, `planner_context_recent_message_max_chars`, `planner_memory_item_max_chars`, `planner_attempt_history_limit`, `planner_attempt_history_max_chars`, `planner_attempt_message_max_chars`, `planner_prior_result_max_chars`, `planner_ambient_record_limit`。
    * 语义校验与证据预算：`semantic_checker_attempt_limit`, `semantic_checker_args_max_chars`, `semantic_checker_facts_max_chars`, `semantic_checker_message_max_chars`, `semantic_checker_evidence_summary_max_chars`, `semantic_checker_verdict_error_chars`, `semantic_checker_verdict_retries`, `task_result_preview_chars`。
    * 模型传输重试与锁心跳：`provider_transport_max_retries`, `provider_transport_retry_min_seconds`, `provider_transport_retry_max_seconds`, `execution_lease_min_seconds`, `execution_lease_heartbeat_max_seconds`。
  * **工具、探测器与多渠道 Connector 超参外显化**：
    * 守护探针与日志：`default_port_probe_timeout_seconds`, `daemon_project_event_concurrency`, `max_git_status_prompt_chars`, `max_log_read_bytes`, `max_log_prompt_chars`。
    * 技能与搜索：`provider_error_max_chars`, `skill_file_max_bytes`, `search_title_max_chars`, `search_snippet_max_chars`, `search_response_max_bytes`, `search_x_response_max_bytes`。
    * 连接器通道超时：`connector_idle_timeout_seconds`, `connector_heartbeat_interval_seconds`, `telegram_get_file_timeout_seconds`, `telegram_download_timeout_seconds`, `weixin_config_timeout_seconds`, `weixin_context_token_max_age_seconds`, `weixin_ingress_stale_after_seconds`。
  * **因果域定向 Prompt 语义变异（Domain-Driven Semantic Mutation）**：
    * 沙盒自博弈在反传归因问责后，依据具体失败域（Planner 语法、Checker 幻觉证据不足、循环未收敛、安全策略违规）智能匹配特化的自然语言提示词增量补丁，实现类似 TextGrad 机制的模型自我修复演进。

```
+=============================================================================+
|             NAVI NEURAL AGENTIC NETWORK: CLOSED-LOOP TOPOLOGY               |
+=============================================================================+
|                                                                             |
|      [ User Input / Trigger Event ]                                         |
|                    |                                                        |
|                    v                                                        |
|   +------------------------------------+                                    |
|   | 1. FORWARD PASS (PROMPT COMPILER)  |                                    |
|   |    - 7 Dynamic Prompt Layers       |                                    |
|   |    - Bounded Facts (chars/depth)   |<---------+                         |
|   |    - Dynamic Context Recall Pool   |          |                         |
|   +------------------------------------+          |                         |
|                    |                              |                         |
|                    v                              |                         |
|   +------------------------------------+          |                         |
|   | 2. ZERO-ELSE EXECUTION GRAPH       |          | [ Continuous Dynamic    |
|   |    - Pure Algebraic Routing        |          |   Parameter Updates     |
|   |    - Saga Lease Allocation         |          |   via SQLite Registry ] |
|   |    - Tool & Sub-agent Dispatch     |          |                         |
|   +------------------------------------+          |                         |
|                    |                              |                         |
|                    v                              |                         |
|   +------------------------------------+          |                         |
|   | 3. TRACE & TERMINAL EVALUATION     |          |                         |
|   |    - Outcome (Success/Degraded/...) |          |                         |
|   |    - Dynamic R_T Reward Mapping    |          |                         |
|   +------------------------------------+          |                         |
|                    |                              |                         |
|                    v                              |                         |
|   +------------------------------------+          |                         |
|   | 4. BACKWARD CAUSAL ATTRIBUTION     |          |                         |
|   |    - TD Discounting (gamma^k)      |          |                         |
|   |    - Hebbian LTP / Forgetting      |          |                         |
|   |    - Tool Credit / Blame           |          |                         |
|   |    - Parameter Gradient Emitted    |----------+                         |
|   +------------------------------------+                                    |
|                    |                                                        |
|                    v                                                        |
|   +------------------------------------+                                    |
|   | 5. SHADOW SELF-PLAY ARENA          |                                    |
|   |    - Perturbation Sampling         |                                    |
|   |    - Golden Benchmark Verification |                                    |
|   |    - Auto-Promotion to Registry    |------------------------------------>
|   +------------------------------------+                                    |
+=============================================================================+
```




