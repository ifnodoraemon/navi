"""Navi local-first personal agent OS."""

__version__ = "2.0.5"

import logging
from contextvars import ContextVar

# Global context var for trace tracking
current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="")

class TraceContextFilter(logging.Filter):
    def filter(self, record):
        tid = current_trace_id.get()
        if tid and isinstance(record.msg, str) and not record.msg.startswith("[trace:"):
            record.msg = f"[trace:{tid}] {record.msg}"
        return True

# Apply to root logger so all logs capture the trace context
logging.getLogger().addFilter(TraceContextFilter())
