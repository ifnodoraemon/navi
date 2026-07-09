# Navi AGI Architecture Blueprint (下一代 Agent OS 终极蓝图)

本蓝图结合了业界最前沿的架构设计（Claude Code, Devin, Hermes）与 Navi 的 6 大顶层架构断层，以“The Zen of Navi”为核心哲学，彻底重构现有的单体脚本模型，打造真正具备“心智、免疫与进化”能力的超级代理生态。

---

## 一、 暗脑机制与语义图谱 (The Subconscious & Semantic Graph)
*解决痛点：系统完全被动响应、记忆缺乏逻辑关联（单纯依赖 FTS5 文本检索）*

- **后台潜意识 (Background Daemon)**：引入脱离主线程的“暗脑 (Dark Brain)”守护进程。在系统无用户请求（Idle）时，暗脑会自动在后台运行记忆压缩 (Context Compaction)、垃圾回收、以及针对历史失败案例的演算复盘。
- **动态知识图谱 (Hierarchical Knowledge Graph)**：废弃扁平的文本存储。构建包含 `episode`（情景）, `fact`（事实）, `preference`（偏好）, `constraint`（红线约束）的多维关联图谱。
- **遗忘曲线与自我净化**：系统对记忆引入“置信度衰减率”。如果某条工作偏好长时间未被激活或被用户多次纠正，系统将降低其权重甚至打上“反面知识 (Negative Memory)”标签，保证大模型 Context 永远锐利、精准。

## 二、 微内核与子代理集群 (Microkernel & Sub-Agent Swarm)
*解决痛点：单体 Loop 过载、认知带宽瓶颈*

- **调度器微内核化**：将目前全能且臃肿的单一 `StateGraph` 退化为极简的“调度器 (Dispatcher)”。主干只负责任务拆解、意图识别与验收（Checker）。
- **原生多体协同 (Swarm Intelligence)**：面对如“重构代码”等复杂目标，主进程动态生成并拉起多个专职 Sub-agent（如：只读代码的 `Researcher`、专职写代码的 `Coder`、以及负责审查和跑测试的 `Reviewer`）。由主节点作为把关人，彻底解放单一上下文窗口的算力上限。

## 三、 硬件级隔离沙箱 (Containerized Execution Harness)
*解决痛点：沙箱“裸奔”，缺乏物理级别的防爆半径控制*

- **重构 Harness 隔离层**：淘汰目前仅依赖 Timeout 和 Prompt 拦截的软防线。所有的外部网络请求、文件系统写操作、命令执行，默认在 ephemeral (用完即抛) 的 Docker 或轻量级微虚拟机 (Firecracker) 中进行。
- **AST 级高精度操作**：废弃粗暴的纯文本正则表达式替换。当模型修改代码时，强制使用基于抽象语法树 (AST) 的工具链，从根源上消灭缩进错误和局部破坏，实现真正的外科手术级修复。

## 四、 持续对齐与进化角斗场 (RLAIF & Evolutionary Sandbox)
*解决痛点：系统无状态，失败后无法在系统层面吸收教训，缺乏演进机制*

- **负反馈闭环 (RLAIF Pipeline)**：用户的每一次打回、中断、或对执行结果的否定，都会被系统提取并转换为结构化的偏好数据（DPO Pairs）和红线约束，注入系统记忆网络。
- **进化角斗场 (GAN Checkers)**：当 Navi 生成了自我改进的代码补丁（如新的核心路由或 Prompt 架构），绝对禁止直接原地热更新主进程。系统会在后台拉起隔离的“Navi-Beta”，提取历史库中 100 个最高难度的 Trace 进行压力测试。只有在所有 Checker 证明 Beta 版本的胜率高于当前版本时，系统才会自动合并分支，实现基于证据的无损自进化。

## 五、 副驾驶级人机交互 (Copilot-Grade HITL)
*解决痛点：二元对立的审批流，人机交互颗粒度过粗*

- **细粒度方向盘热切换 (Interactive Steerability)**：打破传统的 `pause_for_approval`（只允许 Yes/No）模式。当系统遇到安全边界或重大架构选择卡点时，通过类似 Claude Code 的交互式 Bash 终端，允许用户直接接管 Agent 的影子工作区 (Shadow Workspace)，手动微调 AST 补丁或输入两行关键命令。完成微调后，系统重新接管剩余 Loop。实现真正意义上的人机结对编程。

---

### 结语：从工具走向生命体

通过后台暗脑（维持心智健康）、沙箱集群（确保执行力与安全）、进化角斗场（保证架构向善），以及细粒度的人机融合，这份蓝图将使 Navi 彻底从“基于 Prompt 的脚本程序”跃迁为“极具生命力和自主适应性的 AGI 操作系统”。
