import re

with open("src/navi/runs/__init__.py", "r") as f:
    content = f.read()

content = content.replace("    \"EXECUTION_LOGS_TABLE\",\n", "")
content = content.replace("    \"Watch\",\n", "")

with open("src/navi/runs/__init__.py", "w") as f:
    f.write(content)
