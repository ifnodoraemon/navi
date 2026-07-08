import re

with open("src/navi/runs/store.py", "r") as f:
    content = f.read()

content = content.replace("from ._execution_log_store import EXECUTION_LOGS_TABLE, TOOL_CALL_LOGS_TABLE, ExecutionLogStoreMixin", "from ._execution_log_store import TOOL_CALL_LOGS_TABLE, ExecutionLogStoreMixin")

content = content.replace("    EXECUTION_LOGS_TABLE,", "")

with open("src/navi/runs/store.py", "w") as f:
    f.write(content)
