import re

with open("src/navi/acceptance.py", "r") as f:
    content = f.read()

# Remove ExecutionLog import
content = content.replace("from .runs import ExecutionLog, RunStore", "from .runs import RunStore")

# Remove _latest_protocol entirely
content = re.sub(r"def _latest_protocol\((?:.|\n)*?return \{\}\n\n", "", content)

# Remove _log_facts entirely
content = re.sub(r"def _log_facts\((?:.|\n)*?    \}\n\n", "", content)

# In _state_snapshot, replace list_execution_logs with tool_calls or just omit
content = re.sub(r"    logs = runs\.list_execution_logs\(run_id, limit=200\)\n", "", content)
content = re.sub(r"        \"log_count\": len\(logs\),\n", "        \"log_count\": 0,\n", content)
content = re.sub(r"        \"last_log_exit_code\": logs\[0\]\.exit_code if logs else None,\n", "        \"last_log_exit_code\": None,\n", content)

# Find where _latest_protocol was called
# Wait, I didn't see where it was called. Let's just remove the calls too.
content = re.sub(r"    protocol = _latest_protocol\(runs\.list_execution_logs\(run_id, limit=200\), phase=phase\)\n", "    protocol = {}\n", content)
content = re.sub(r"        \"logs\": \[_log_facts\(log\) for log in runs\.list_execution_logs\(run\.id, limit=10\)\],\n", "        \"logs\": [],\n", content)

with open("src/navi/acceptance.py", "w") as f:
    f.write(content)
