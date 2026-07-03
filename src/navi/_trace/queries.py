"""Trace query layer: re-exports from the split modules.

Historically a single 805-line module mixing redaction, run-view
construction, evaluation rules, and schema. Split into:

- :mod:`navi.trace.redaction` — ``_redact``, ``_redact_json_text``
- :mod:`navi.trace.run_views` — ``_trace_run_views``, ``_event_run_view``, blob helpers
- :mod:`navi.trace.evaluation_rules` — ``_evaluate_trace_with_rules`` and rule functions
- :mod:`navi.trace.schema` — table definitions and ``_ensure_schema_current``

This module re-exports the public surface so existing importers
(``store.py``, API routers) keep working unchanged.
"""

from __future__ import annotations

from .evaluation_rules import (
    TRACE_EVALUATION_RULES,
    _base_trace_evidence,
    _capability_result_has_safeguard_decision,
    _capability_result_is_approval_request,
    _capability_result_is_input_schema_mismatch,
    _checker_failure_rule,
    _converged_loop_decision_rule,
    _evaluate_trace_with_rules,
    _failed_loop_decision_rule,
    _first_failure,
    _first_failure_rule,
    _has_approval_required_pause,
    _loop_decision_events,
    _loop_decision_rule,
    _loop_decision_summary,
    _loop_results_include,
    _missing_trace_rule,
    _planner_call_started_without_result,
    _planner_failure_rule,
    _planner_no_response_rule,
    _record_first_failure_evidence,
    _record_recovery_evidence,
    _runtime_failure_rule,
    _safeguard_failure_rule,
    _successful_completion_after,
)
from .redaction import _redact, _redact_json_text
from .run_views import (
    _event_input,
    _event_output,
    _event_run_type,
    _event_run_view,
    _event_trace_run_status,
    _extract_blobs,
    _resolve_blobs,
    _trace_run_views,
)
from .schema import (
    TRACE_BLOBS_TABLE,
    TRACE_EVALUATIONS_TABLE,
    TRACE_EVENTS_TABLE,
    _TRACE_EVENT_COLUMNS,
    _ensure_schema_current,
    _table_schema,
)

__all__ = [
    "TRACE_BLOBS_TABLE",
    "TRACE_EVALUATION_RULES",
    "TRACE_EVALUATIONS_TABLE",
    "TRACE_EVENTS_TABLE",
    "_TRACE_EVENT_COLUMNS",
    "_base_trace_evidence",
    "_capability_result_has_safeguard_decision",
    "_capability_result_is_approval_request",
    "_capability_result_is_input_schema_mismatch",
    "_checker_failure_rule",
    "_converged_loop_decision_rule",
    "_ensure_schema_current",
    "_evaluate_trace_with_rules",
    "_event_input",
    "_event_output",
    "_event_run_type",
    "_event_run_view",
    "_event_trace_run_status",
    "_extract_blobs",
    "_failed_loop_decision_rule",
    "_first_failure",
    "_first_failure_rule",
    "_has_approval_required_pause",
    "_loop_decision_events",
    "_loop_decision_rule",
    "_loop_decision_summary",
    "_loop_results_include",
    "_missing_trace_rule",
    "_planner_call_started_without_result",
    "_planner_failure_rule",
    "_planner_no_response_rule",
    "_redact",
    "_redact_json_text",
    "_record_first_failure_evidence",
    "_record_recovery_evidence",
    "_resolve_blobs",
    "_runtime_failure_rule",
    "_safeguard_failure_rule",
    "_successful_completion_after",
    "_table_schema",
    "_trace_run_views",
]
