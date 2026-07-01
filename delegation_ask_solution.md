# 委托任务交互（ask.user）的完整解决方案

针对后台任务（Delegation Run）在执行时调用 `ask.user` 导致直接 failed（`execution produced an ask action...`）的问题，以下是基于架构原则的**完整端到端解决方案**。

该方案的**核心思想**是：让后台任务能够优雅地暂停并抛出问题，然后让主 Agent (Planner) 负责理解用户的自然语言回答，并通过工具将回答“转交”给后台任务使其恢复执行。这避免了任何硬编码的字符串拦截，完全遵循 Agent 架构意图驱动的原则。

---

## 第一部分：引入挂起状态 (Lifecycle)

我们需要在生命周期中增加一个明确的“等待输入”状态，这样任务可以暂停而不是失败。

**文件修改：`navi/src/navi/lifecycle.py`**
```python
class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"   # 新增状态
    COMPLETED = "completed"
    FAILED = "failed"

RUN_STATUS_AWAITING_INPUT = RunStatus.AWAITING_INPUT

# 将其加入活跃状态，让 Planner 的 current_state 能够观测到它
RUN_ACTIVE_STATUSES = frozenset({RUN_STATUS_PENDING, RUN_STATUS_RUNNING, RUN_STATUS_AWAITING_INPUT})
```

---

## 第二部分：任务挂起并发送提问 (Execution Engine)

当任务执行产生 `yields_control=True` 时，将状态转为 `AWAITING_INPUT`，并把问题主动推给用户。

**文件修改：`navi/src/navi/execution.py`**
```python
# 修改 1：在状态判断处
def _execution_status_from_turn_result(result) -> tuple[str, str]:
    facts = result.facts if isinstance(result.facts, dict) else {}
    if getattr(result, "yields_control", False):
        # 以前是 RUN_STATUS_FAILED，现在改为 AWAITING_INPUT
        return RUN_STATUS_AWAITING_INPUT, "execution suspended awaiting user input"
    ...

# 修改 2：在 _execute_herness_engine 处，如果状态是 AWAITING_INPUT，向外部发事件
execution_status, status_reason = self._execution_status_from_turn_result(turn_result)

if execution_status == RUN_STATUS_AWAITING_INPUT:
    # 任务暂停，把代理想问的问题广播给用户的客户端
    from .event_bus import ResponseReadyEvent
    import asyncio
    asyncio.create_task(
        self.event_bus.publish(
            ResponseReadyEvent(
                peer_id=task.peer_id,
                sender_id=task.sender_id,
                source=task.source,
                text=turn_result.text,
                session_alias=f"connector:{task.source}:{task.peer_id}"
            )
        )
    )
    
    # 类似 pending 状态的处理，挂起子代理并更新数据库
    self.subagents.finish(
        subagent_run.id,
        status=SUBAGENT_STATUS_SUSPENDED,
        output_data={"exit_code": 0, "summary": turn_result.text},
        error="",
    )
    task_suspended = self.runs.get(task.id)
    self.runs.update_run(task.id, status=RUN_STATUS_AWAITING_INPUT)
    return task_suspended
```

---

## 第三部分：主 Agent 恢复任务 (Planner & Capability)

用户回答后，消息会由 `ConnectorRouter` 发给主 Agent (Planner)。由于我们已经在第一部分将其加入了 `RUN_ACTIVE_STATUSES`，主 Agent 在 `current_state` 里会看到：
> "当前有一个任务处于 awaiting_input 状态，它最后问了：[你的简历在哪个目录]"

我们需要给主 Agent 一个动作，让它能把用户的回答传回去并恢复任务。

**1. 新增工具定义：`navi/src/navi/actions/specs.py`**
```python
    capability_spec(
        name="delegate.send_input",
        description="向处于 awaiting_input 状态的后台委托任务发送用户提供的输入信息，使其恢复执行。",
        schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "目标任务的 run_id"},
                "message": {"type": "string", "description": "要发送给该任务的具体回答或信息"}
            },
            "required": ["run_id", "message"]
        }
    )
```

**2. 实现 Capability：`navi/src/navi/actions/delegation.py`**
```python
from .specs import capability
from ..lifecycle import RUN_STATUS_AWAITING_INPUT, RUN_STATUS_PENDING

@capability("delegate.send_input")
class DelegateSendInputCapability(BaseCapability):
    async def invoke(self, args: dict[str, Any], *, permission: str, context: CapabilityContext) -> CapabilityResult:
        run_id = args.get("run_id")
        message = args.get("message")
        if not run_id or not message:
            raise SchemaMismatch("delegate.send_input requires run_id and message.")

        # 检查任务状态
        target_run = context.runtime.runs.get(run_id)
        if not target_run or target_run.status != RUN_STATUS_AWAITING_INPUT:
            return CapabilityResult(ok=False, observation=f"Run {run_id} is not awaiting input.")

        # 将用户的回答写入该任务专属的 Executor Session
        # 这样后台任务恢复时，LLM能看到用户的回答
        session_id = context.runtime.memory.get_or_create_session(f"executor:{run_id}")
        context.runtime.memory.add_message(session_id, "user", message)

        # 唤醒任务：将状态改为 PENDING 放入队列，让执行引擎 (Daemon) 下一次循环时自动拉起执行
        context.runtime.runs.update_run(run_id, status=RUN_STATUS_PENDING)

        return CapabilityResult(
            ok=True,
            observation=f"Successfully sent input to run {run_id}. The task is now resuming.",
        )
```
