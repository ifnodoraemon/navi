from pathlib import Path
from typing import Mapping

from ..capabilities_types import Capability
from ..tools import ToolSpec

def get_action_handlers(home: Path, project_dir: Path, specs: Mapping[str, ToolSpec], action_handler_keys: Mapping[str, str]) -> dict[str, Capability]:
    import importlib

    def _load(module_name: str, class_name: str):
        module = importlib.import_module(f"navi.actions.{module_name}")
        return getattr(module, class_name)

    factories = {
        "final_answer": lambda spec: _load("conversation", "FinalAnswerCapability")(spec),
        "clarify": lambda spec: _load("conversation", "ClarifyCapability")(spec),
        "delegate_spawn": lambda spec: _load("delegation", "DelegateSpawnCapability")(
            spec, home=home, project_dir=project_dir
        ),
        "delegate_prepare": lambda spec: _load("delegation", "DelegatePrepareCapability")(spec, home=home),
        "approval_request": lambda spec: _load("approval", "ApprovalRequestCapability")(spec, home=home),
        "delegate_run": lambda spec: _load("delegation", "DelegateRunCapability")(spec, home=home),
        "watch_create": lambda spec: _load("watch", "WatchCreateCapability")(
            spec, home=home, project_dir=project_dir
        ),
        "delegate_delete": lambda spec: _load("delegation", "DelegateDeleteCapability")(spec, home=home),
        "watch_delete": lambda spec: _load("watch", "WatchDeleteCapability")(spec, home=home),
        "session_create": lambda spec: _load("session", "SessionCreateCapability")(
            spec, home=home
        ),
        "session_request_elevation": lambda spec: _load("session", "SessionRequestElevationCapability")(
            spec, home=home
        ),
        "memory_add": lambda spec: _load("memory", "MemoryAddCapability")(spec, home=home),
        "trace_evaluate": lambda spec: _load("trace", "TraceEvaluateCapability")(
            spec, home=home
        ),
        "evolution_propose": lambda spec: _load("evolution", "EvolutionProposeCapability")(
            spec, home=home
        ),
        "evolution_record_evaluation": lambda spec: _load(
            "evolution", "EvolutionRecordEvaluationCapability"
        )(spec, home=home),
        "evolution_apply": lambda spec: _load("evolution", "EvolutionApplyCapability")(
            spec, home=home
        ),
        "evolution_rollback": lambda spec: _load("evolution", "EvolutionRollbackCapability")(
            spec, home=home
        ),
        "approval_resolve": lambda spec: _load("approval", "ApprovalResolveCapability")(spec, home=home),
        "execution_retry": lambda spec: _load("delegation", "ExecutionRetryCapability")(spec, home=home),
        "workflow_propose": lambda spec: _load("workflow", "WorkflowProposeCapability")(
            spec, home=home, project_dir=project_dir
        ),
        "workflow_approve": lambda spec: _load("workflow", "WorkflowApproveCapability")(spec, home=home),
        "workflow_run": lambda spec: _load("workflow", "WorkflowRunCapability")(
            spec, home=home, project_dir=project_dir
        ),
        "workflow_verify": lambda spec: _load("workflow", "WorkflowVerifyCapability")(spec, home=home),
        "workflow_resume": lambda spec: _load("workflow", "WorkflowRunCapability")(
            spec, home=home, project_dir=project_dir, resume=True
        ),
        "workflow_status": lambda spec: _load("workflow", "WorkflowStatusCapability")(spec, home=home),
    }

    handlers = {}
    for name, handler_key in action_handler_keys.items():
        factory = factories.get(handler_key)
        if factory is None:
            raise ValueError(f"unknown action capability handler: {handler_key}")
        handlers[name] = factory(specs[name])
    return handlers
