import re

with open("src/navi/state_graph.py", "r") as f:
    content = f.read()

# Add CapabilityRecoveryPort class
recovery_port_class = """
class CapabilityRecoveryPort:
    \"\"\"Recovery node port that turns failed capability executions into retry facts.\"\"\"

    def recover(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
    ) -> ReflectionDecision:
        recovery_facts = {
            "trigger": "capability.failed",
            "reason_code": "execution_failed",
            "blocked": False,
            "failure_domain": "executor",
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "goal_id": spec.goal_id,
            "error_reason": executed.error_reason,
            "message": executed.message,
            "facts": executed.facts,
        }
        return ReflectionDecision(
            retry=state.attempt < spec.retry_policy.max_attempts,
            reason_code="execution_failed",
            facts={
                "recovery": recovery_facts,
                "recovery_fact": json.dumps(
                    {
                        "fact_type": "capability_execution_failed",
                        "facts": recovery_facts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        )

class RecoveryReflectorPort:
"""

content = content.replace("class RecoveryReflectorPort:\n", recovery_port_class)

# Modify DurableStateGraphRunner init
init_find = """        executor_port: CapabilityExecutorPort | None = None,
        reflector_port: RecoveryReflectorPort | None = None,
    ):
        self.home = home
        self.store = LoopRunStore(home)
        self.gateway = gateway or GlobalResourceGateway(ResourceLimits(max_concurrent=1))
        self.harness = harness or Harness(home=home)
        self.checker = checker or DeterministicChecker()
        self.planner_port = planner_port
        self.executor_port = executor_port
        self.reflector_port = reflector_port or RecoveryReflectorPort()"""

init_replace = """        executor_port: CapabilityExecutorPort | None = None,
        reflector_port: RecoveryReflectorPort | None = None,
        recovery_port: CapabilityRecoveryPort | None = None,
    ):
        self.home = home
        self.store = LoopRunStore(home)
        self.gateway = gateway or GlobalResourceGateway(ResourceLimits(max_concurrent=1))
        self.harness = harness or Harness(home=home)
        self.checker = checker or DeterministicChecker()
        self.planner_port = planner_port
        self.executor_port = executor_port
        self.reflector_port = reflector_port or RecoveryReflectorPort()
        self.recovery_port = recovery_port or CapabilityRecoveryPort()"""

content = content.replace(init_find, init_replace)

# Replace the capability failed logic
execute_failed_find = """            if not executed.ok:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="capability_failed",
                    evidence=executed.to_dict(),
                )
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="checker_rejected",
                    terminal_state=LoopTerminalState.FAILED,
                    evidence={"reason": "executor_capability_failed"},
                )
                self.gateway.release()
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )"""

execute_failed_replace = """            if not executed.ok:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="capability_failed",
                    evidence=executed.to_dict(),
                )
                decision = self.recovery_port.recover(spec, state, executed=executed)
                collected_evidence["reflection"] = decision.to_dict()
                if decision.retry:
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="new_route_available",
                        evidence=decision.to_dict(),
                    )
                    self.gateway.release()
                    return await self.run_async(
                        spec,
                        workspace=workspace,
                        run_id=state.run_id,
                        evidence=collected_evidence,
                    )
                else:
                    state = self._transition(
                        state,
                        node=LoopNode.REFLECT,
                        condition="no_route_available",
                        terminal_state=LoopTerminalState.FAILED,
                        evidence=decision.to_dict(),
                    )
                    self.gateway.release()
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )"""

content = content.replace(execute_failed_find, execute_failed_replace)

with open("src/navi/state_graph.py", "w") as f:
    f.write(content)
