import re

with open("src/navi/runs/_execution_log_store.py", "r") as f:
    content = f.read()

content = content.replace("from .models import ExecutionLog, ToolCallLog", "from .models import ToolCallLog")

# Remove EXECUTION_LOGS_TABLE
content = re.sub(r"EXECUTION_LOGS_TABLE = Table\(\n(?:.|\n)*?    \],\n\)\n", "", content)

# Remove add_execution_log
content = re.sub(r"    def add_execution_log\((?:.|\n)*?                \)\n        return log\n\n", "", content)

# Remove list_execution_logs
content = re.sub(r"    def list_execution_logs\((?:.|\n)*?        return \[ExecutionLog\(\*row\) for row in rows\]\n\n", "", content)

with open("src/navi/runs/_execution_log_store.py", "w") as f:
    f.write(content)
