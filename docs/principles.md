# Navi Non-Negotiable Principles (The Zen of Navi)

本宪法是 Navi 系统的第一性原理。我们抛弃了又长又臭的操作手册和历史包袱，将其提炼为 5 大哲学支柱。
任何架构变更、功能增加和自进化行为，如果与以下原则相悖，则必须被无情驳回。

---

### 1. 架构即智能 (Agentic by Architecture)
智能来自于底层系统的自由度与机制设计，而非生硬的“提示词规矩”。
- **支架，而非硬编码 (Scaffolding, Not Hardcoding)**：给模型提供事实 (Facts) 和能力 (Capabilities)，让它自己决定路由。绝不允许在代码或 Prompt 中写死产品业务流。
- **工具仅提供事实 (Tools Return Facts Only)**：工具的作用是感知环境或执行改变，只能返回结构化的数据，绝不能越权提供“下一步建议”或“决策”。
- **本地优先与终端解耦 (Local-First & Connector-Agnostic)**：核心引擎永远是纯净的。绝对不准将微信、飞书、Telegram 等外部渠道的特性硬编码进核心逻辑。
- **CLI 优先 (CLI First)**：如果一个能力无法在没有 UI 的命令行下独立、无状态地跑通，那它就不配成为一个核心 Capability。

### 2. 状态优于直觉 (State Over Vibes)
大模型的判断（Vibes）是脆弱的，只有写入数据库的状态（State）才是坚不可摧的。
- **审批是硬状态 (Approval Is State)**：用户在聊天框里发一句“我授权你”，仅仅是对话。真正的权限下发必须通过 Durable StateGraph 的硬性状态机流转。
- **上下文压缩底线 (Context Compression)**：当历史记忆太长需要摘要压缩时，可以丢弃对话，但“禁止事项 (Constraints)”、“安全红线”和“待审批项”必须百分之百保留。
- **环境真理在本地 (Environment Truth Is Local)**：模型不准“脑补”用户的系统环境。在执行前，必须用代码真实去探明路径、OS 版本和服务状态。

### 3. 代码即事实 (Code is Truth, Docs are Context)
系统用代码运行，不用 Markdown 运行。文档只记录最高共识，拒绝维护无用的历史废纸。
- **拒绝历史债务 (No Historical Compatibility Debt)**：不兼容旧格式就大胆报错，立刻删掉旧代码。绝不为了“向后兼容旧的错误架构”而写一堆丑陋的适配器。
- **全局设计优于局部补丁 (Global Design Before Patch)**：遇到由于工具引发的个别错误，去修工具的代码和 Schema！不准在 Global Prompt 里加上形如“遇到某某工具报错请如何如何”的肮脏补丁。
- **文档的阅后即焚 (Ephemeral Process Docs)**：那些头脑风暴、Bug 修理计划、TODO 追踪表，在代码写完落地的瞬间，必须立刻删除，保持文档库的绝对纯净。

### 4. 进化必须受控 (Governed Evolution)
不仅是代码，AI 的记忆和思维方式也不能像黑盒一样静默突变。
- **记忆是操作系统，不是记事本 (Memory is an OS, Not a Notebook)**：废弃纯文本大乱炖。记忆必须被区分为 `fact`, `preference`, `constraint`, `procedure` 等硬性类型。必须支持置信度衰减、溯源、和“反面教材 (Negative memory)”。
- **审计先行 (Audit First & Reversible)**：无论是模型自重写源码，还是执行敏感命令，必须留下带有时间戳、差异比对 (Diff) 和回滚方案的防篡改记录。
- **目标不等于自我保护 (Goals Must Not Become Self-Protection)**：AI 的终极意义是服务用户。当系统的“安全拦截”与“完成用户目标”冲突时，宁可任务失败，也绝不能越权去破坏安全限制。

### 5. 正交与最小特权 (Orthogonality & Least Privilege)
不要相信任何单一环节（包括最聪明的大模型）。用系统层面的层层设卡来兜底安全。
- **严格的拓展边界 (Strict Extension Boundaries)**：
  - `tools`: 只管执行和返回事实。
  - `capabilities`: 稳定的外露契约接口。
  - `skills`: 提供思路、方法论和工作流指南（不包含代码）。
  - `plugins`: 安装含有外部依赖的真实能力代码。
  - `hooks`: 只负责生命周期的拦截、记录与放行。
  它们各司其职，绝不允许互相包含或跨界调用。
- **深度防御 (Defense in Depth)**：大模型只是一层。系统必须额外提供权限天花板 (Permission ceilings)、沙箱 (Sandboxing)、AST 级别的高精度管控。所有获取的外部信息默认不可信。
